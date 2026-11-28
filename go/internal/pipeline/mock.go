package pipeline

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"time"

	invisiscanv1 "github.com/Squid2333/invisiscan-ai/gen/go/invisiscan/v1"
	"github.com/Squid2333/invisiscan-ai/go/internal/config"
	"github.com/Squid2333/invisiscan-ai/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// mock.go 是阶段四的回放驱动：假帧源、确定性跟踪桩，以及把两者接进事件 FSM 的循环。

// 回放参数：帧数、节奏和跟踪桩阈值，与 python/perception/mock_server.py 的脚本一一对应。
const (
	// mockFrameCount 与 python/perception/mock_server.py 的脚本对齐：第 1、2 帧造一个
	// 只有两次输出的待确认事件，第 23–30 帧造确认后结束的事件，第 50 帧补一个低分框。
	mockFrameCount = 70
	// mockFrameInterval 是 8 FPS，与 seed 相机配置的帧率一致。
	mockFrameInterval = time.Second / 8
	// mockTrackScore 模拟跟踪桩的高分阈值：低于它的框不进观测，但仍算"画面里有人"，
	// 因为事件 FSM 只看原始检测数量。
	mockTrackScore = 0.1
	// mockTrackMissed 是允许的连续丢帧数，超过后桩才把 ID 报成 removed。
	mockTrackMissed = 3
)

// mockFrames 生成回放时间线。所有帧共用同一块全零 BGR24 缓冲：帧只被读取，而 70 份
// 1440×2560 的副本会白白吃掉几百 MB 内存。
type mockFrames struct {
	start time.Time
	image *invisiscanv1.Image
}

// newMockFrames 以 start 为起点锚定模拟的 RTCP 时钟，并按相机分辨率预分配一块 BGR24 图。
func newMockFrames(start time.Time, width, height int32) mockFrames {
	return mockFrames{start: start, image: &invisiscanv1.Image{
		Encoding: invisiscanv1.Image_ENCODING_RAW_BGR24,
		Data:     make([]byte, int(width)*int(height)*3),
		Width:    width,
		Height:   height,
	}}
}

// frame 生成从 1 起始的序号对应的请求。帧时间每帧递增 mockFrameInterval，
// 让 FSM 看到稳定的 125ms 节奏。
func (s mockFrames) frame(seq uint64) *invisiscanv1.FrameRequest {
	return &invisiscanv1.FrameRequest{
		FrameSeq:  seq,
		FrameTime: timestamppb.New(s.start.Add(time.Duration(seq-1) * mockFrameInterval)),
		Image:     s.image,
	}
}

// mockTracker 是按左右半幅分道的确定性桩，不是真正的跟踪器。
// 场景里每半幅最多一个人，用通道号当 ID 就能与检测返回顺序无关；阶段三会换成 BOTSORT。
type mockTracker struct {
	lastSeen [2]uint64
}

// Update 把每个检测框分到 1 号（左半）或 2 号（右半）通道，并让 ID 在 mockTrackMissed
// 帧空档内继续存活。
// 低于 mockTrackScore 的框在这里被丢弃，不会进入观测——第 50 帧考的就是"检测框非空但
// 被跟踪桩忽略"这种情形。
func (t *mockTracker) Update(frame *invisiscanv1.FrameResult) ([]observation, []int32) {
	var outputs []observation
	for _, det := range frame.Detections {
		if det.Score < mockTrackScore {
			continue
		}
		lane := 0
		if det.Box.X1 >= float32(frame.InferWidth)/2 {
			lane = 1
		}
		t.lastSeen[lane] = frame.FrameSeq
		outputs = append(outputs, observation{ID: int32(lane + 1), Box: det.Box, Score: det.Score, Depth: det.Depth})
	}
	var removed []int32
	for lane, last := range t.lastSeen {
		if last != 0 && frame.FrameSeq-last > mockTrackMissed {
			removed = append(removed, int32(lane+1))
			t.lastSeen[lane] = 0
		}
	}
	return outputs, removed
}

// RunMock 回放 mockFrameCount 帧，直到跑完或 ctx 被取消。
//
// startup 只用于约束建流：它被转成建流阶段的取消信号，mock 服务没起来时快速失败而不是
// 挂死。帧由 ticker 控速，第一帧立即发出。写库用脱离取消的 context 加 10s 超时，所以
// Ctrl-C 落在两帧之间，不会把事务截断。退出时结束所有轨迹并把事件以 "shutdown" 关闭，
// 保证被打断的回放也能留下可读的库状态。
//
// 假帧尺寸直接取 A 库中该相机的 width/height（存储值），不在这里应用 rotation。
func RunMock(ctx, startup context.Context, cfg config.Config, s *store.Store, client invisiscanv1.PerceptionServiceClient, logger *slog.Logger) error {
	width, height, err := s.EnabledCameraDimensions(startup, cfg.MockCameraID)
	if err != nil {
		return fmt.Errorf("find enabled mock camera %d: %w", cfg.MockCameraID, err)
	}
	if int64(width)*int64(height) > int64(cfg.MaxMessageBytes)/3 {
		return fmt.Errorf("mock camera %d BGR frames exceed max_message_bytes", cfg.MockCameraID)
	}

	streamCtx, cancelStream := context.WithCancel(ctx)
	defer cancelStream()
	stopStartup := context.AfterFunc(startup, cancelStream)
	stream, err := openPerception(streamCtx, client, fmt.Sprintf("%s/camera/%d", cfg.InstanceID, cfg.MockCameraID))
	stopStartup()
	if err != nil {
		return err
	}

	fsm := newEventFSM(s, cfg, logger, &mockTracker{})
	// 帧时间是从本次回放起点算的模拟 RTCP 时间线，只在建源时锚定一次。
	source := newMockFrames(time.Now().UTC(), width, height)
	logger.Info("mock replay started", "camera_id", cfg.MockCameraID, "perception_target", cfg.PerceptionTarget)
	ticker := time.NewTicker(mockFrameInterval)
	defer ticker.Stop()
	var processed uint64
	for seq := uint64(1); seq <= mockFrameCount && ctx.Err() == nil; seq++ {
		if seq > 1 {
			select {
			case <-ctx.Done():
			case <-ticker.C:
			}
			if ctx.Err() != nil {
				break
			}
		}
		frame := source.frame(seq)
		result, err := perceiveFrame(stream, frame)
		if err != nil {
			if ctx.Err() != nil {
				break
			}
			return err
		}
		// 用脱离取消的 context 完成本次写库，收到停止信号时不截断事务。
		writeCtx, cancelWrite := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
		err = fsm.step(writeCtx, frame, result)
		cancelWrite()
		if err != nil {
			return fmt.Errorf("persist frame %d: %w", seq, err)
		}
		processed++
	}
	shutdown, cancelShutdown := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
	defer cancelShutdown()
	if err := fsm.closeEvent(shutdown, "shutdown", "shutdown"); err != nil {
		return err
	}
	if ctx.Err() == nil {
		if err := stream.CloseSend(); err != nil {
			return fmt.Errorf("close perception stream: %w", err)
		}
		if _, err := stream.Recv(); err != io.EOF {
			if err == nil {
				return fmt.Errorf("unexpected response after final mock frame")
			}
			return fmt.Errorf("finish perception stream: %w", err)
		}
	}
	logger.Info("mock replay stopped", "frames", processed)
	return nil
}

// openPerception 用 WaitForReady 建流，给容器留出接受连接的时间；随后发送服务端要求
// 先到的 SessionConfig，收到 SessionReady 才算建流成功。
func openPerception(ctx context.Context, client invisiscanv1.PerceptionServiceClient, streamID string) (invisiscanv1.PerceptionService_PerceiveClient, error) {
	stream, err := client.Perceive(ctx, grpc.WaitForReady(true))
	if err != nil {
		return nil, fmt.Errorf("open perception stream: %w", err)
	}
	if err := stream.Send(&invisiscanv1.PerceiveRequest{Payload: &invisiscanv1.PerceiveRequest_Config{
		Config: &invisiscanv1.SessionConfig{StreamId: streamID},
	}}); err != nil {
		return nil, fmt.Errorf("send perception config: %w", err)
	}
	response, err := stream.Recv()
	if err != nil {
		return nil, fmt.Errorf("receive perception ready: %w", err)
	}
	if response.GetReady() == nil {
		return nil, fmt.Errorf("first perception response was not SessionReady")
	}
	return stream, nil
}

// perceiveFrame 发一帧并取回结果。回包的 frame_seq 或 frame_time 与请求不一致说明流已
// 错位，这里按协议错误处理，不做容忍。
func perceiveFrame(stream invisiscanv1.PerceptionService_PerceiveClient, frame *invisiscanv1.FrameRequest) (*invisiscanv1.FrameResult, error) {
	if err := stream.Send(&invisiscanv1.PerceiveRequest{Payload: &invisiscanv1.PerceiveRequest_Frame{Frame: frame}}); err != nil {
		return nil, fmt.Errorf("send frame %d: %w", frame.FrameSeq, err)
	}
	response, err := stream.Recv()
	if err != nil {
		return nil, fmt.Errorf("receive frame %d: %w", frame.FrameSeq, err)
	}
	result := response.GetResult()
	if result == nil || result.FrameSeq != frame.FrameSeq || !result.FrameTime.AsTime().Equal(frame.FrameTime.AsTime()) {
		return nil, fmt.Errorf("unexpected perception response for frame %d", frame.FrameSeq)
	}
	return result, nil
}
