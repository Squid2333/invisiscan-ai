// events.go 是这条流水线的“脑子”：每来一帧，它判断画面里有没有人、谁算同一个人，
// 再决定这些信息该不该记、记成什么样，最后交给 store 写进数据库。
// 现在喂进来的是假画面和假跟踪器；等阶段三换成真摄像头和真跟踪器，这个文件不用改。
package pipeline

import (
	"context"
	"fmt"
	"log/slog"
	"math"
	"time"

	invisiscanv1 "github.com/Squid2333/invisiscan-ai/gen/go/invisiscan/v1"
	"github.com/Squid2333/invisiscan-ai/go/internal/config"
	"github.com/Squid2333/invisiscan-ai/go/internal/store"
	"github.com/google/uuid"
)

// tracker 是“跟踪器”的接口。给它一帧检测框，它告诉你两件事：这一帧里这些人分别是谁
// （用一个固定 ID 认人），以及哪些 ID 已经跟丢了。
// 现在接的是个假货（mock.go 里的 mockTracker），阶段三换成真的 BOTSORT 时，这个文件不用动。
type tracker interface {
	Update(*invisiscanv1.FrameResult) (observations []observation, removed []int32)
}

// observation 是跟踪器认出来的“一个人”：框在画面里的位置、这个框有多可信，
// 以及这一帧算出来的深度。深度可能是 NaN，表示这一帧没算出来（人离得太远之类）。
type observation struct {
	ID    int32
	Box   *invisiscanv1.BBox
	Score float32
	Depth float32
}

// eventState 是正在攒的这一次“有人经过”。
// id：进库之后拿到的编号，第一条轨迹确认之前是 0，也就是“还没进库”。
// uuid：提前办好的身份证。假回放时会按“相机 + 起始帧号 + 回放标识”算成一个固定值，
// 这样重启多少次算出来的都是同一个，才能判断“这次回放是不是已经写过了”。
// absentFrames：连着几帧没看到人，数够了就把这次事件关掉。
// replayed：这次回放上一遍已经完整写过库了，这一遍只跟着时间线走，不再重复记。
type eventState struct {
	id           int64
	uuid         uuid.UUID
	replayed     bool // 上一遍回放已经把这次事件写进库了。
	absentFrames int
}

// trackState 是某一个跟踪器 ID 的小账本：出现过几次（count）、最后一次是哪一帧（lastSeen）、
// 深度平滑到多少（depth），以及还有哪些点没写进库（points）。
//
// points 在确认前后意思不一样：确认之前，它是“这人到底算不算真的”的证据，先攒着不下笔；
// 确认之后，它就是等着发货的点，攒够一批就写进库。
type trackState struct {
	id       int64
	count    int32
	lastSeen time.Time
	depth    float64
	points   []store.Point
}

// eventFSM 就是这台状态机本身：喂它一帧，它负责决定事件开不开、哪条轨迹该确认、哪条该收尾。
// 它只会被一条协程一帧一帧地调（mock.go 里按帧号顺序调），不会同时进来两帧，所以内部没有加锁。
type eventFSM struct {
	store   *store.Store
	cfg     config.Config
	logger  *slog.Logger
	tracker tracker
	event   *eventState
	tracks  map[int32]*trackState
}

// newEventFSM 建一台新的状态机。刚建好时手里既没有事件、也没有任何轨迹，
// 都是跑到需要时才现建，所以同一台状态机能一个接一个地处理同一个相机的多次事件。
func newEventFSM(s *store.Store, cfg config.Config, logger *slog.Logger, tr tracker) *eventFSM {
	return &eventFSM{store: s, cfg: cfg, logger: logger, tracker: tr, tracks: make(map[int32]*trackState)}
}

// step 是“处理一帧”的总入口：frame 带着原图尺寸（用来把坐标换算回原图），
// result 带着这一帧的检测框和帧时间。
//
// 里面这几步的顺序不能随便换：
//  1. 先判断事件开不开。这里看的是“有没有检测框”，而不是“跟踪器认没认出来”——
//     所以一个分数低到被跟踪器丢掉的人，仍然算“画面里有人”，能把缺席计数清零。
//  2. 让跟踪器认人，拿到这一帧的观测，以及它判定已经跟丢的 ID。
//  3. 事件开着的时候，把每条观测记到对应的轨迹上。
//  4. 被跟踪器丢掉的 ID，对应轨迹收尾。
//  5. 已经进库的轨迹，手里攒够 200 个点、或者跨度到了 1 秒，就先写一批进库。
func (f *eventFSM) step(ctx context.Context, frame *invisiscanv1.FrameRequest, result *invisiscanv1.FrameResult) error {
	if len(result.Detections) > 0 {
		if f.event == nil {
			event := &eventState{uuid: uuid.New()}
			if f.cfg.MockReplayID != "" {
				// 假回放不随机生成身份证，而是按“相机 + 这次从第几帧开始 + 回放标识”算一个固定值：
				// 同一段回放跑多少遍，算出来的 UUID 都一样，于是能查出“这次是不是已经写过了”。
				event.uuid = uuid.NewSHA1(uuid.NameSpaceOID, fmt.Appendf(nil, "invisiscan/mock/%d/%d/%s", f.cfg.MockCameraID, frame.FrameSeq, f.cfg.MockReplayID))
				var err error
				// 库里已经有这个事件，说明上一遍已经跑完并提交了，这一遍就只走时间线、不再写库。
				event.replayed, err = f.store.EventExists(ctx, event.uuid)
				if err != nil {
					return err
				}
			}
			f.event = event
		}
		f.event.absentFrames = 0
	} else if f.event != nil {
		f.event.absentFrames++
		if f.event.absentFrames == f.cfg.MaxAbsentFrames {
			if err := f.closeEvent(ctx, "absence", "lost"); err != nil {
				return err
			}
		}
	}

	observations, removed := f.tracker.Update(result)
	frameTime := result.FrameTime.AsTime()
	if f.event != nil && !f.event.replayed {
		// 这一遍是重复回放，库里已经有了，就不再记点，只让时间线继续往下走。
		sx := float64(frame.Image.Width) / float64(result.InferWidth)
		sy := float64(frame.Image.Height) / float64(result.InferHeight)
		for _, obs := range observations {
			if err := f.record(ctx, obs, frameTime, sx, sy); err != nil {
				return err
			}
		}
	}
	for _, id := range removed {
		if err := f.endTrack(ctx, f.tracks[id], "lost"); err != nil {
			return err
		}
		delete(f.tracks, id)
	}
	for _, track := range f.tracks {
		if track != nil && track.id != 0 && len(track.points) > 0 &&
			(len(track.points) >= 200 || frameTime.Sub(track.points[0].FrameTime) >= time.Second) {
			if err := f.flush(ctx, track); err != nil {
				return err
			}
		}
	}
	return nil
}

// record 把跟踪器认出的一个人记到他的轨迹上。如果这已经是第 3 次看到他（MinTrackFrames），
// 就把这条轨迹正式存进库，同时把事件也一起建出来。
//
// 几个容易踩的点：
//   - 事件关掉之后，跟踪器可能还挂着旧 ID。那些 ID 属于上一次事件，它们的检测直接丢掉，
//     不能算到新事件头上。
//   - 坐标不是原样存：先在推理用的那张图上取整，再按比例放大回原图，和旧代码写出来的数一致。
//   - 深度也不是原样存，是按“0.8 旧 + 0.2 新”平滑后的值，留四位小数；
//     整条轨迹一次有效深度都没拿到时写 0，和旧代码保持一致。
func (f *eventFSM) record(ctx context.Context, obs observation, frameTime time.Time, sx, sy float64) error {
	track, exists := f.tracks[obs.ID]
	if exists && track == nil {
		// 事件关了之后跟踪器还挂着的旧 ID，属于上一次事件，这次的检测直接丢掉。
		return nil
	}
	if !exists {
		track = &trackState{depth: math.NaN()}
		f.tracks[obs.ID] = track
	}

	// 深度不是直接用，而是和之前的值做加权平均，避免单帧抖动。本帧是 NaN 或无穷就不参与。
	depth := float64(obs.Depth)
	if !math.IsNaN(depth) && !math.IsInf(depth, 0) {
		if math.IsNaN(track.depth) {
			track.depth = depth
		} else {
			track.depth = f.cfg.DepthEMAAlpha*track.depth + (1-f.cfg.DepthEMAAlpha)*depth
		}
	}
	var pointDepth float32
	if !math.IsNaN(track.depth) {
		// 旧代码是保留四位小数写进轨迹文件的，这里照做。
		pointDepth = float32(math.RoundToEven(track.depth*1e4) / 1e4)
	}
	// 旧代码是先把坐标取整、再放大回原图，这里照做。
	x1 := int32(float64(int(obs.Box.X1)) * sx)
	y1 := int32(float64(int(obs.Box.Y1)) * sy)
	x2 := int32(float64(int(obs.Box.X2)) * sx)
	y2 := int32(float64(int(obs.Box.Y2)) * sy)
	track.points = append(track.points, store.Point{
		Seq: track.count, FrameTime: frameTime, BBoxX: x1, BBoxY: y1, BBoxW: x2 - x1, BBoxH: y2 - y1,
		Depth: pointDepth, DetScore: &obs.Score,
	})
	track.count++
	track.lastSeen = frameTime
	// 还没攒够 3 次（MinTrackFrames），先不写库，免得白记一堆一闪而过的人。
	if track.count != f.cfg.MinTrackFrames {
		return nil
	}

	created, err := f.store.CreateConfirmedTrack(ctx, store.ConfirmedTrack{
		EventUUID: f.event.uuid.String(), TrackUUID: uuid.NewString(), CameraID: f.cfg.MockCameraID, TrackerID: obs.ID,
		StartedAt: track.points[0].FrameTime, ClockSource: "rtcp", Points: track.points,
	})
	if err != nil {
		return fmt.Errorf("confirm tracker ID %d: %w", obs.ID, err)
	}
	if f.event.id == 0 {
		f.logger.Info("event opened", "camera_id", f.cfg.MockCameraID, "event_id", created.EventID)
	}
	f.event.id = created.EventID
	track.id = created.TrackID
	track.points = track.points[:0]
	return nil
}

// flush 把手里攒着的点一次性写进库，然后把缓冲清空，等下一批。
// 只有已经确认进库的轨迹才能这么写；还没确认的轨迹走不到这里。
func (f *eventFSM) flush(ctx context.Context, track *trackState) error {
	if err := f.store.AppendTrackPoints(ctx, track.id, track.points); err != nil {
		return fmt.Errorf("flush points for track %d: %w", track.id, err)
	}
	track.points = track.points[:0]
	return nil
}

// endTrack 给一条轨迹收尾：先把还没写的点写完，再记上结束原因和结束时间。
// 两种情况直接跳过——压根没有这条轨迹（nil），或者它从来没被确认过（id == 0），
// 后者在库里本来就没有行，没什么可改的。
// 结束时间用“最后一次看见它的那一帧”，不是“发现它不见的那一帧”，
// 否则轨迹会被硬拉长到它根本没出现过的画面上。
func (f *eventFSM) endTrack(ctx context.Context, track *trackState, reason string) error {
	if track == nil || track.id == 0 {
		return nil
	}
	if len(track.points) > 0 {
		if err := f.flush(ctx, track); err != nil {
			return err
		}
	}
	_, err := f.store.EndTrack(ctx, track.id, "confirmed", reason, track.lastSeen)
	return err
}

// closeEvent 结束这一次事件：把还活着的轨迹都收尾，再把事件本身标成关闭。
// 轨迹收尾之后这里只把它们标成 nil、不从表里删掉，是为了等到跟踪器又报同一个 ID 时，
// record 一看就知道“这人属于上一次事件”，把检测丢掉，而不是当成新人重新开一条轨迹。
// 如果这次事件从头到尾一个人都没确认过（id == 0），库里没有事件行，也就没得关。
func (f *eventFSM) closeEvent(ctx context.Context, eventReason, trackReason string) error {
	if f.event == nil {
		return nil
	}
	for id, track := range f.tracks {
		if err := f.endTrack(ctx, track, trackReason); err != nil {
			return err
		}
		f.tracks[id] = nil
	}
	if f.event.id != 0 {
		if _, err := f.store.CloseEvent(ctx, f.event.id, eventReason); err != nil {
			return err
		}
		f.logger.Info("event closed", "event_id", f.event.id, "reason", eventReason)
	}
	f.event = nil
	return nil
}
