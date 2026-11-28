package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	db "github.com/Squid2333/invisiscan-ai/go/internal/store/db"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

// Point 是轨迹上的“一个点”：某一帧里，这个人在画面的什么位置、什么时候被看到、离镜头多远。
// 一条轨迹在 tracks 表里只有一行，它的每个点各占 track_points 表的一行。
// DetScore 用指针，是因为老数据允许“没打分”，这种情况要能区分于“分数就是 0”。
type Point struct {
	Seq       int32
	FrameTime time.Time
	BBoxX     int32
	BBoxY     int32
	BBoxW     int32
	BBoxH     int32
	Depth     float32
	DetScore  *float32
}

// ConfirmedTrack 是“这条轨迹我认了”时，一次性交给数据库的全部材料：
// 事件和轨迹各自的 UUID、哪个相机、跟踪器给的 ID、从哪一帧开始、时间从哪来，
// 以及确认之前攒下的那几个点。
// 调用方不用自己先建事件——这里会按相机找现有的进行中事件，没有就新建一个。
type ConfirmedTrack struct {
	EventUUID   string
	TrackUUID   string
	CameraID    int64
	TrackerID   int32
	StartedAt   time.Time
	ClockSource string
	Points      []Point
}

// ConfirmedTrackResult 是建完之后还回来的两个编号：事件 ID 和轨迹 ID。
// 后面追加点、结束轨迹、去查这条数据时都要用到，所以调用方得把它们记住。
type ConfirmedTrackResult struct {
	EventID int64
	TrackID int64
}

// Track 是从库里读回来的一条轨迹（只有骨架，不含具体的点），主要给测试和排查用。
// EndedAt 和 EndReason 是指针：轨迹还没结束时它们是 nil，而不是零值时间或空字符串，
// 这样一眼就能看出“还没结束”。
type Track struct {
	ID          int64
	UUID        string
	EventID     int64
	CameraID    int64
	TrackerID   int32
	StartedAt   time.Time
	EndedAt     *time.Time
	FrameCount  int32
	Status      string
	EndReason   *string
	ClockSource string
}

// CreateConfirmedTrack 把一条“刚确认”的轨迹一次性落库，一个事务里干四件事：
// 找事件（没有就新建）、把这个事件里的轨迹数加一、插入轨迹行、把确认前攒下的点写进去。
// 四件事全成功才提交；中间任何一步失败都整体回滚，不会留下“事件建了但轨迹没建”这种半截数据。
//
// 一进来先给相机加锁，是为了让同一路视频流不会同时开出两个“进行中”的事件。
// 如果这条轨迹的开始时间比事件现有的开始时间还早，事件开始时间会被往回调成更早那个——
// 事件应该从画面里第一个人的第一帧算起，而不是从第二条轨迹确认的那一刻算起。
//
// 返回值里带着事件 ID 和轨迹 ID；相机不存在（或已被删掉）时返回 ErrNotFound。
func (s *Store) CreateConfirmedTrack(ctx context.Context, in ConfirmedTrack) (out ConfirmedTrackResult, err error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return out, fmt.Errorf("begin confirmed track: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	queries := s.queries.WithTx(tx)

	// 锁住相机，同一路视频流只能排队建事件，不会并发开出两个“进行中”的事件。
	if _, err = queries.LockCamera(ctx, in.CameraID); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return out, ErrNotFound
		}
		return out, fmt.Errorf("lock camera: %w", err)
	}
	eventID, err := queries.GetOpenEventForCamera(ctx, in.CameraID)
	createdEvent := false
	if errors.Is(err, pgx.ErrNoRows) {
		var eventUUID pgtype.UUID
		if err = eventUUID.Scan(in.EventUUID); err != nil {
			return out, fmt.Errorf("parse event uuid: %w", err)
		}
		eventID, err = queries.CreateEvent(ctx, db.CreateEventParams{
			Uuid: eventUUID, CameraID: in.CameraID,
			StartedAt: pgtype.Timestamptz{Time: in.StartedAt, Valid: true},
		})
		if err != nil {
			return out, fmt.Errorf("insert event: %w", err)
		}
		createdEvent = true
	} else if err != nil {
		return out, fmt.Errorf("find open event: %w", err)
	} else {
		err = queries.UpdateOpenEventForTrack(ctx, db.UpdateOpenEventForTrackParams{
			ID: eventID, StartedAt: pgtype.Timestamptz{Time: in.StartedAt, Valid: true},
		})
		if err != nil {
			return out, fmt.Errorf("update event: %w", err)
		}
	}
	if createdEvent {
		if err = queries.SetNewEventTrackCount(ctx, eventID); err != nil {
			return out, err
		}
	}
	var trackUUID pgtype.UUID
	if err = trackUUID.Scan(in.TrackUUID); err != nil {
		return out, fmt.Errorf("parse track uuid: %w", err)
	}
	out.TrackID, err = queries.CreateTrack(ctx, db.CreateTrackParams{
		Uuid: trackUUID, EventID: eventID, CameraID: in.CameraID, TrackerID: in.TrackerID,
		StartedAt:   pgtype.Timestamptz{Time: in.StartedAt, Valid: true},
		ClockSource: in.ClockSource, FrameCount: int32(len(in.Points)),
	})
	if err != nil {
		return out, fmt.Errorf("insert track: %w", err)
	}
	if len(in.Points) > 0 {
		if err = copyTrackPoints(ctx, tx, out.TrackID, in.Points); err != nil {
			return out, err
		}
	}
	if err = tx.Commit(ctx); err != nil {
		return out, fmt.Errorf("commit confirmed track: %w", err)
	}
	out.EventID = eventID
	return out, nil
}

// AppendTrackPoints 给一条已经确认过的轨迹“续点”。
// 一个人在画面里待着的时候，轨迹会一帧一帧往后长，点不能全憋在内存里等轨迹结束才写，
// 所以每攒一批就追加一次。一批通常就是 1 秒或 200 个点。
//
// 先后顺序有讲究：先把这条轨迹的帧数加上本批的点数，再批量写点。那次更新顺便把这一行锁住，
// 如果轨迹已经被结束掉了，更新会命中 0 行，这里就直接返回 ErrNotFound 并回滚，
// 保证不会把点写进一条已经关掉的轨迹里。
func (s *Store) AppendTrackPoints(ctx context.Context, trackID int64, points []Point) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin append track points: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	// 这次更新同时起到“锁住这条轨迹”的作用，挡住并发的收尾操作。
	updated, err := s.queries.WithTx(tx).AddConfirmedTrackFrames(ctx, db.AddConfirmedTrackFramesParams{
		ID: trackID, FrameCount: int32(len(points)),
	})
	if err != nil {
		return fmt.Errorf("update track frame count: %w", err)
	}
	if updated == 0 {
		return ErrNotFound
	}
	if err := copyTrackPoints(ctx, tx, trackID, points); err != nil {
		return err
	}
	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit appended track points: %w", err)
	}
	return nil
}

// copyTrackPoints 是“批量写点”的小工具：把一批点一次塞进 track_points 表。
// 用的是 CopyFrom（等价于 PostgreSQL 的 COPY），比一条条 INSERT 快得多——
// 几千个点也只要一次网络往返。
// 它只负责写，不管事务：事务由调用方开，所以“新建轨迹”和“追加点”两条路径都能共用它。
func copyTrackPoints(ctx context.Context, tx pgx.Tx, trackID int64, points []Point) error {
	_, err := tx.CopyFrom(ctx, pgx.Identifier{"track_points"}, []string{
		"track_id", "seq", "frame_time", "bbox_x", "bbox_y", "bbox_w", "bbox_h", "depth", "det_score",
	}, pgx.CopyFromSlice(len(points), func(i int) ([]any, error) {
		p := points[i]
		return []any{trackID, p.Seq, p.FrameTime, p.BBoxX, p.BBoxY, p.BBoxW, p.BBoxH, p.Depth, p.DetScore}, nil
	}))
	if err != nil {
		return fmt.Errorf("copy track points: %w", err)
	}
	return nil
}

// EndTrack 给一条轨迹收尾：写状态、写结束原因、写结束时间。
// 它要求调用方说明“我期望这条轨迹现在是什么状态”，只有状态还对得上才会改。
// 这样同一条轨迹不会被收尾两次、也不会把第一次的结束原因覆盖掉。
// 返回 true 表示真的改到了 1 行；false 表示条件没命中，多半是别处已经把它结束了。
func (s *Store) EndTrack(ctx context.Context, trackID int64, expectedStatus, reason string, endedAt time.Time) (bool, error) {
	result, err := s.queries.EndTrack(ctx, db.EndTrackParams{
		ID: trackID, Status: expectedStatus,
		EndReason: pgtype.Text{String: reason, Valid: true},
		EndedAt:   pgtype.Timestamptz{Time: endedAt, Valid: true},
	})
	if err != nil {
		return false, fmt.Errorf("end track: %w", err)
	}
	return result == 1, nil
}

// GetTrack 按 ID 读一条轨迹的基本信息（不含具体的点），测试和排查时用。
// 查不到就返回 ErrNotFound，和这个包其他方法保持同一套“找不到”的说法。
// 库里是 NULL 的字段（结束时间、结束原因）读出来给 nil，调用方直接判空就行，不用管零值。
func (s *Store) GetTrack(ctx context.Context, id int64) (Track, error) {
	result, err := s.queries.GetTrack(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return Track{}, ErrNotFound
	}
	if err != nil {
		return Track{}, fmt.Errorf("get track: %w", err)
	}
	row := Track{
		ID: result.ID, UUID: result.Uuid.String(), EventID: result.EventID,
		CameraID: result.CameraID, TrackerID: result.TrackerID, StartedAt: result.StartedAt.Time,
		FrameCount: result.FrameCount, Status: result.Status, ClockSource: result.ClockSource,
	}
	if result.EndedAt.Valid {
		row.EndedAt = &result.EndedAt.Time
	}
	if result.EndReason.Valid {
		row.EndReason = &result.EndReason.String
	}
	return row, nil
}

// ListTrackPoints 读一条轨迹的全部点，按点的序号（seq）从小到大排好（排序在 SQL 里做）。
// 点的分数在库里可以是空的，读出来也保持 nil，不会把“没打分”硬变成 0 分。
func (s *Store) ListTrackPoints(ctx context.Context, trackID int64) ([]Point, error) {
	rows, err := s.queries.ListTrackPoints(ctx, trackID)
	if err != nil {
		return nil, fmt.Errorf("list track points: %w", err)
	}
	points := make([]Point, 0, len(rows))
	for _, row := range rows {
		point := Point{
			Seq: row.Seq, FrameTime: row.FrameTime.Time, BBoxX: row.BboxX, BBoxY: row.BboxY,
			BBoxW: row.BboxW, BBoxH: row.BboxH, Depth: row.Depth,
		}
		if row.DetScore.Valid {
			score := row.DetScore.Float32
			point.DetScore = &score
		}
		points = append(points, point)
	}
	return points, nil
}
