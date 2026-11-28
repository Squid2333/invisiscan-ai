package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	db "github.com/Squid2333/invisiscan-ai/go/internal/store/db"
	"github.com/jackc/pgx/v5/pgxpool"
)

// ErrCameraOwned 表示这台相机已经被别人占着了——通常是另一个进程正在跑它的流水线。
var ErrCameraOwned = errors.New("store: camera is already owned by another session")

// CameraSession 是“一台相机的工作台”：它把相机的建议锁和所有写库操作都放在同一条连接上。
//
// 为什么非要同一条连接？因为 PostgreSQL 的建议锁是绑在具体连接上的：锁在 A 连接上拿的，
// 写库却走了 B 连接，那这把锁就白拿了，第二个实例照样能写进来。锁和写同源，才真的挡得住。
//
// 还有个好处：这条连接一断，锁会自动释放，所以进程崩了之后重启就能立刻重新拿到锁。
// 使用上有两条规矩：只能一条一条地用（别开多个 goroutine 并发压它）；相机流水线停了就 Close。
type CameraSession struct {
	*Store
	conn     *pgxpool.Conn
	cameraID int64
}

// AcquireCamera 试着“认领”一台相机：先从连接池借一条独占连接，再在这条连接上试拿相机的建议锁。
// 拿到了就返回一个可以直接用来写库的工作台；拿不到说明别人正在跑这台相机，
// 这时返回 ErrCameraOwned，调用方应该直接退出，不要硬往库里写。
func AcquireCamera(ctx context.Context, pool *pgxpool.Pool, cameraID int64) (*CameraSession, error) {
	conn, err := pool.Acquire(ctx)
	if err != nil {
		return nil, fmt.Errorf("acquire session for camera %d: %w", cameraID, err)
	}
	session := &CameraSession{Store: newStore(conn.Conn()), conn: conn, cameraID: cameraID}
	acquired, err := session.queries.TryOwnCamera(ctx, cameraID)
	if err != nil {
		// 查询本身出错了（比如连接断了）：先把这条会话收拾掉，别留半条烂摊子。
		session.Close()
		return nil, fmt.Errorf("lock camera %d: %w", cameraID, err)
	}
	if !acquired {
		// 没抢到锁：连接得还回池子，不能占着不放，然后再告诉调用方这台相机有人了。
		conn.Release()
		return nil, fmt.Errorf("camera %d: %w", cameraID, ErrCameraOwned)
	}
	return session, nil
}

// Close 把这条连接彻底关掉，而不是还回连接池。
// 因为建议锁跟着连接走：如果只是还回池子，下一个借到这条连接的人就“继承”了这把锁，
// 可那根本不是他的相机。直接关掉最干净，锁当场释放。
func (s *CameraSession) Close() {
	if s.conn == nil {
		return
	}
	// Hijack 是“从池里摘出来，池子不再管它”，这样才有权自己决定什么时候真关。
	conn := s.conn.Hijack()
	s.conn = nil
	// 关闭最多等 5 秒，别因为网络卡住把退出流程一起拖死。
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = conn.Close(ctx)
}

// PendingTrack 是恢复之后还等着下游处理的轨迹（分类、ReID 之类）。
// tracks 表本身就是队列，这里只是把查出来的几列整理成一个好用的结构。
type PendingTrack struct {
	ID       int64
	Status   string
	Category string
}

// Recovery 是这次“收拾现场”的结果：结束了几条轨迹、关了几个事件，
// 以及这台相机现在积压了哪些待处理的轨迹。
type Recovery struct {
	EndedTracks  int64
	ClosedEvents int64
	Pending      []PendingTrack
}

// Reconcile 是重启后要做的第一件事：把一台相机上次没干完的活收尾，再把积压的工作读出来。
// 整个过程在一个事务里，而且只碰自己认领的那台相机，不会去动别人的数据。
//
// 三步：
//  1. 还挂在 confirmed 的轨迹 → 结束掉，原因记 recovered，结束时间取已经写进库的最后一帧；
//  2. 还挂在 open 的事件 → 关闭，原因记 recovered，结束时间取它下面轨迹里最晚的那个；
//  3. 把这台相机待处理的轨迹列出来（已结束的、已分类的室内轨迹、开了 ReID 时待 ReID 的）。
//
// 三步都成功才提交；中途失败就整体回滚，下次启动再来一遍也不会重复收拾。
func (s *CameraSession) Reconcile(ctx context.Context, reidEnabled bool) (Recovery, error) {
	tx, err := s.database.Begin(ctx)
	if err != nil {
		return Recovery{}, fmt.Errorf("begin recovery for camera %d: %w", s.cameraID, err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	queries := s.queries.WithTx(tx)
	// 只处理自己这把锁对应的那一台相机。多相机、多实例部署时，“顺带把别人也收拾了”是很危险的。
	owned := []int64{s.cameraID}
	ended, err := queries.RecoverConfirmedTracks(ctx, owned)
	if err != nil {
		return Recovery{}, fmt.Errorf("recover confirmed tracks: %w", err)
	}
	closed, err := queries.RecoverOpenEvents(ctx, owned)
	if err != nil {
		return Recovery{}, fmt.Errorf("recover open events: %w", err)
	}
	rows, err := queries.ListRecoveredWork(ctx, db.ListRecoveredWorkParams{CameraIds: owned, ReidEnabled: reidEnabled})
	if err != nil {
		return Recovery{}, fmt.Errorf("list recovered work: %w", err)
	}
	result := Recovery{EndedTracks: ended, ClosedEvents: closed}
	for _, row := range rows {
		result.Pending = append(result.Pending, PendingTrack{ID: row.ID, Status: row.Status, Category: row.Category.String})
	}
	if err := tx.Commit(ctx); err != nil {
		return Recovery{}, fmt.Errorf("commit recovery for camera %d: %w", s.cameraID, err)
	}
	return result, nil
}
