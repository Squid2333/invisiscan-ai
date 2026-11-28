package pipeline

import (
	"context"
	"io"
	"log/slog"
	"math"
	"os"
	"testing"
	"time"

	invisiscanv1 "github.com/Squid2333/invisiscan-ai/gen/go/invisiscan/v1"
	"github.com/Squid2333/invisiscan-ai/go/internal/config"
	"github.com/Squid2333/invisiscan-ai/go/internal/store"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// TestMockRealtimePostgres 用真实 PostgreSQL 双库加 mock PerceptionService 跑完整回放。
// 三个环境变量缺一就跳过：它会写两个库，且依赖 dev 容器与复制链路。
//
// 测试相机建在云端库，再轮询等它复制到 A 库——pipeline 只能看见 A 库里的相机，订阅没配
// 好时这里会立刻失败。断言只打在 FSM 状态切换的帧上：确认前库里没有任何行、右侧轨迹确认
// 时首次落库、左侧轨迹确认时事件起点被回拨、第 50 帧低分框把关闭推迟到第 70 帧。
func TestMockRealtimePostgres(t *testing.T) {
	aDSN := os.Getenv("INVISISCAN_TEST_DSN")
	cloudDSN := os.Getenv("INVISISCAN_CLOUD_TEST_DSN")
	target := os.Getenv("INVISISCAN_TEST_PERCEPTION_TARGET")
	if aDSN == "" || cloudDSN == "" || target == "" {
		t.Skip("set INVISISCAN_TEST_DSN, INVISISCAN_CLOUD_TEST_DSN and INVISISCAN_TEST_PERCEPTION_TARGET")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	a, err := pgxpool.New(ctx, aDSN)
	if err != nil {
		t.Fatal(err)
	}
	defer a.Close()
	cloud, err := pgxpool.New(ctx, cloudDSN)
	if err != nil {
		t.Fatal(err)
	}
	defer cloud.Close()
	s := store.New(a)
	const width, height = 1280, 960
	name := "mock-realtime-" + uuid.NewString()
	var cameraID int64
	if err := cloud.QueryRow(ctx, `WITH location AS (INSERT INTO locations(name) VALUES($1) RETURNING id)
		INSERT INTO cameras(location_id,name,stream_url,publish_path,width,height,fps)
		SELECT id,$1,'rtsp://test/live',$1,$2,$3,8 FROM location RETURNING id`, name, width, height).Scan(&cameraID); err != nil {
		t.Fatalf("create cloud fixture camera: %v", err)
	}
	for {
		var replicated bool
		if err := a.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM cameras WHERE id=$1)`, cameraID).Scan(&replicated); err != nil {
			t.Fatal(err)
		}
		if replicated {
			break
		}
		select {
		case <-ctx.Done():
			t.Fatal("fixture camera did not replicate to A")
		case <-time.After(100 * time.Millisecond):
		}
	}

	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	stream, err := openPerception(ctx, invisiscanv1.NewPerceptionServiceClient(conn), name)
	if err != nil {
		t.Fatal(err)
	}
	cfg := config.Config{MockCameraID: cameraID, MinTrackFrames: 3, MaxAbsentFrames: 20, DepthEMAAlpha: 0.8}
	fsm := newEventFSM(s, cfg, slog.New(slog.NewTextHandler(io.Discard, nil)), &mockTracker{})
	source := newMockFrames(time.Date(2026, 11, 28, 0, 0, 0, 0, time.UTC), width, height)
	frameTime := func(seq uint64) time.Time { return source.frame(seq).FrameTime.AsTime() }

	var eventID int64
	for seq := uint64(1); seq <= mockFrameCount; seq++ {
		frame := source.frame(seq)
		result, err := perceiveFrame(stream, frame)
		if err != nil {
			t.Fatal(err)
		}
		if err := fsm.step(ctx, frame, result); err != nil {
			t.Fatalf("step frame %d: %v", seq, err)
		}
		switch seq {
		case 2, 22, 25:
			if pending := fsm.event != nil; pending != (seq != 22) {
				t.Fatalf("frame %d: pending event=%t", seq, pending)
			}
			var events, tracks int
			if err := a.QueryRow(ctx, `SELECT (SELECT count(*) FROM events WHERE camera_id=$1),
				(SELECT count(*) FROM tracks WHERE camera_id=$1)`, cameraID).Scan(&events, &tracks); err != nil {
				t.Fatal(err)
			}
			if events != 0 || tracks != 0 {
				t.Fatalf("frame %d wrote tentative data: events=%d tracks=%d", seq, events, tracks)
			}
		case 26:
			if err := a.QueryRow(ctx, `SELECT id FROM events WHERE camera_id=$1`, cameraID).Scan(&eventID); err != nil {
				t.Fatalf("event missing at first confirmation: %v", err)
			}
			assertEvent(t, ctx, s, eventID, "open", 1, frameTime(24))
		case 28, 49, 50, 69:
			assertEvent(t, ctx, s, eventID, "open", 2, frameTime(23))
		case 70:
			event := assertEvent(t, ctx, s, eventID, "closed", 2, frameTime(23))
			if event.EndedAt == nil || !event.EndedAt.Equal(frameTime(30)) || event.ClosedReason == nil || *event.ClosedReason != "absence" {
				t.Fatalf("event ended at the wrong time or for the wrong reason: %+v", event)
			}
		}
	}
	if err := stream.CloseSend(); err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Recv(); err != io.EOF {
		t.Fatalf("expected end of perception stream, got %v", err)
	}

	var events, tracks int
	if err := a.QueryRow(ctx, `SELECT (SELECT count(*) FROM events WHERE camera_id=$1),
		(SELECT count(*) FROM tracks WHERE camera_id=$1)`, cameraID).Scan(&events, &tracks); err != nil {
		t.Fatal(err)
	}
	if events != 1 || tracks != 2 {
		t.Fatalf("replay wrote %d events and %d tracks, want 1 and 2", events, tracks)
	}
	for _, want := range []struct {
		id     int32
		frames []uint64
		depths []float32
		x      int32
	}{
		{1, []uint64{23, 27, 28, 29, 30}, []float32{0, 2.5, 2.7, 2.7, 3}, 128},
		{2, []uint64{24, 25, 26}, []float32{3, 3, 3}, 768},
	} {
		var trackID int64
		if err := a.QueryRow(ctx, `SELECT id FROM tracks WHERE event_id=$1 AND tracker_id=$2`, eventID, want.id).Scan(&trackID); err != nil {
			t.Fatal(err)
		}
		track, err := s.GetTrack(ctx, trackID)
		if err != nil {
			t.Fatal(err)
		}
		if track.Status != "ended" || track.EndReason == nil || *track.EndReason != "lost" || track.FrameCount != int32(len(want.frames)) ||
			track.ClockSource != "rtcp" || !track.StartedAt.Equal(frameTime(want.frames[0])) ||
			track.EndedAt == nil || !track.EndedAt.Equal(frameTime(want.frames[len(want.frames)-1])) {
			t.Fatalf("unexpected track: %+v", track)
		}
		points, err := s.ListTrackPoints(ctx, trackID)
		if err != nil || len(points) != len(want.frames) {
			t.Fatalf("track %d has %d points, want %d: %v", want.id, len(points), len(want.frames), err)
		}
		for i, point := range points {
			if point.Seq != int32(i) || !point.FrameTime.Equal(frameTime(want.frames[i])) ||
				point.BBoxX != want.x || point.BBoxY != 192 || point.BBoxW != 257 || point.BBoxH != 385 ||
				math.Abs(float64(point.Depth-want.depths[i])) > 1e-5 || point.DetScore == nil || *point.DetScore != 0.9 {
				t.Fatalf("track %d point %d has unexpected data: %+v", want.id, i, point)
			}
		}
	}
}

// assertEvent 读回事件并校验状态、轨迹数、起点，以及 open 状态下 ended_at 仍为空。
func assertEvent(t *testing.T, ctx context.Context, s *store.Store, id int64, status string, count int32, startedAt time.Time) store.Event {
	t.Helper()
	event, err := s.GetEvent(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	if event.Status != status || event.TrackCount != count || !event.StartedAt.Equal(startedAt) || (status == "open" && event.EndedAt != nil) {
		t.Fatalf("event=%+v, want status=%s count=%d start=%v", event, status, count, startedAt)
	}
	return event
}
