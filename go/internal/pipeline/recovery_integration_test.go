package pipeline

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/Squid2333/invisiscan-ai/go/internal/store"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/wait"
)

func TestPostgresCrashRecovery(t *testing.T) {
	if os.Getenv("INVISISCAN_TESTCONTAINERS") != "1" {
		t.Skip("set INVISISCAN_TESTCONTAINERS=1 to run Docker-backed recovery tests")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Minute)
	t.Cleanup(cancel)
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatal(err)
	}
	dsn, pool, target := startRecoveryServices(t, ctx, root)

	binary := filepath.Join(t.TempDir(), "pipeline")
	build := exec.CommandContext(ctx, "go", "build", "-o", binary, "./go/cmd/pipeline")
	build.Dir = root
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build pipeline: %v\n%s", err, output)
	}

	// One camera belongs to a live pipeline, the other has open work but no
	// owner. Recovery of a third camera must leave both untouched.
	heldID := recoveryCamera(t, ctx, pool, "held")
	held, err := store.AcquireCamera(ctx, pool, heldID)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(held.Close)
	heldTrack := recoveryTrack(t, ctx, held.Store, heldID)
	unownedID := recoveryCamera(t, ctx, pool, "unowned")
	unownedTrack := recoveryTrack(t, ctx, store.New(pool), unownedID)

	for _, test := range []struct {
		name, trigger            string
		before                   replayRows
		points                   int
		eventReason, trackReason string
	}{
		{
			name:        "confirmation",
			trigger:     "BEFORE INSERT ON track_points FOR EACH ROW WHEN (NEW.seq = 0)",
			points:      8,
			eventReason: "absence", trackReason: "lost",
		},
		{
			name:        "append points",
			trigger:     "BEFORE INSERT ON track_points FOR EACH ROW WHEN (NEW.seq = 3)",
			before:      replayRows{events: 1, tracks: 2, points: 6, ended: 1},
			points:      6,
			eventReason: "recovered", trackReason: "recovered",
		},
		{
			name:        "close event",
			trigger:     "BEFORE UPDATE ON events FOR EACH ROW WHEN (NEW.status = 'closed')",
			before:      replayRows{events: 1, tracks: 2, points: 8, ended: 2},
			points:      8,
			eventReason: "recovered", trackReason: "lost",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			cameraID := recoveryCamera(t, ctx, pool, test.name)
			workerDSN := *dsn
			query := workerDSN.Query()
			query.Set("application_name", "invisiscan-recovery-worker")
			// Detect the disconnection while the backend is blocked on the trigger.
			query.Set("client_connection_check_interval", "100ms")
			workerDSN.RawQuery = query.Encode()
			start := func() *exec.Cmd {
				cmd := exec.CommandContext(ctx, binary, "-config", "")
				cmd.Env = append(os.Environ(),
					"INVISISCAN_INSTANCE_ID=recovery-test",
					"INVISISCAN_DATABASE_URL="+workerDSN.String(),
					"INVISISCAN_PERCEPTION_TARGET="+target,
					"INVISISCAN_MAX_MESSAGE_BYTES=16777216", "INVISISCAN_STARTUP_TIMEOUT=10s",
					fmt.Sprintf("INVISISCAN_MOCK_CAMERA_ID=%d", cameraID),
					"INVISISCAN_MOCK_REPLAY_ID=crash-test",
					"INVISISCAN_MIN_TRACK_FRAMES=3", "INVISISCAN_MAX_ABSENT_FRAMES=20",
					"INVISISCAN_DEPTH_EMA_ALPHA=0.8",
				)
				return cmd
			}

			crashPipeline(t, ctx, dsn.String(), pool, start(), test.trigger, cameraID, test.before)
			var eventID int64
			if test.before.events != 0 {
				if err := pool.QueryRow(ctx, "SELECT id FROM events WHERE camera_id = $1", cameraID).Scan(&eventID); err != nil {
					t.Fatal(err)
				}
			}
			for i := 0; i < 2; i++ {
				if output, err := start().CombinedOutput(); err != nil {
					t.Fatalf("restart %d: %v\n%s", i+1, err, output)
				}
				if got := readReplayRows(t, ctx, pool, cameraID); got != (replayRows{1, 2, test.points, 2}) {
					t.Fatalf("restart %d: unexpected replay rows %+v", i+1, got)
				}
				var currentEventID int64
				var reasonsMatch bool
				err := pool.QueryRow(ctx, `SELECT e.id, e.track_count = 2 AND e.closed_reason = $2
					AND e.ended_at = (SELECT max(t.ended_at) FROM tracks t WHERE t.event_id = e.id)
					AND (SELECT t.end_reason = $3 FROM tracks t WHERE t.event_id = e.id AND t.tracker_id = 1)
					AND NOT EXISTS (SELECT 1 FROM tracks t WHERE t.event_id = e.id AND
						(t.ended_at IS DISTINCT FROM (SELECT max(p.frame_time) FROM track_points p WHERE p.track_id = t.id)
						OR t.frame_count <> (SELECT count(*) FROM track_points p WHERE p.track_id = t.id)))
					FROM events e WHERE e.camera_id = $1 AND e.status = 'closed'`,
					cameraID, test.eventReason, test.trackReason).Scan(&currentEventID, &reasonsMatch)
				if err != nil || !reasonsMatch {
					t.Fatalf("restart %d: recovery reasons, timestamps or frame counts do not match: %v", i+1, err)
				}
				if eventID != 0 && eventID != currentEventID {
					t.Fatalf("restart %d replaced event %d with %d", i+1, eventID, currentEventID)
				}
				eventID = currentEventID
			}

			session, err := store.AcquireCamera(ctx, pool, cameraID)
			if err != nil {
				t.Fatal(err)
			}
			defer session.Close()
			recovery, err := session.Reconcile(ctx, false)
			if err != nil || recovery.EndedTracks != 0 || recovery.ClosedEvents != 0 || len(recovery.Pending) != 2 ||
				recovery.Pending[0].Status != "ended" || recovery.Pending[1].Status != "ended" {
				t.Fatalf("expected a stable two-track backlog: %+v, %v", recovery, err)
			}
			for _, untouched := range []int64{heldTrack.TrackID, unownedTrack.TrackID} {
				got, err := store.New(pool).GetTrack(ctx, untouched)
				if err != nil || got.Status != "confirmed" {
					t.Fatalf("other camera's track changed: %+v, %v", got, err)
				}
			}
			for _, untouched := range []int64{heldTrack.EventID, unownedTrack.EventID} {
				got, err := store.New(pool).GetEvent(ctx, untouched)
				if err != nil || got.Status != "open" {
					t.Fatalf("other camera's event changed: %+v, %v", got, err)
				}
			}
		})
	}
}

func startRecoveryServices(t *testing.T, ctx context.Context, root string) (*url.URL, *pgxpool.Pool, string) {
	t.Helper()
	pg := runRecoveryContainer(t, ctx, testcontainers.ContainerRequest{
		Image: "pgvector/pgvector:pg16",
		Env: map[string]string{
			"POSTGRES_DB": "recovery", "POSTGRES_USER": "invisiscan", "POSTGRES_PASSWORD": "invisiscan",
		},
		ExposedPorts: []string{"5432/tcp"},
		WaitingFor:   wait.ForLog("database system is ready to accept connections").WithOccurrence(2),
	})
	pgEndpoint, err := pg.PortEndpoint(ctx, "5432/tcp", "")
	if err != nil {
		t.Fatal(err)
	}
	dsn := &url.URL{Scheme: "postgres", User: url.UserPassword("invisiscan", "invisiscan"),
		Host: pgEndpoint, Path: "/recovery", RawQuery: "sslmode=disable"}
	pool, err := pgxpool.New(ctx, dsn.String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	for _, name := range []string{"0001_init.sql", "0003_contract_checks.sql"} {
		script, err := os.ReadFile(filepath.Join(root, "migrations", name))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := pool.Exec(ctx, string(script)); err != nil {
			t.Fatalf("apply %s: %v", name, err)
		}
	}

	// Stage only the checked-in Python sources and generated stubs used by the Dockerfile.
	buildDir := t.TempDir()
	for _, pattern := range []string{"python/perception/*", "python/gen/invisiscan/v1/*.py"} {
		paths, err := fs.Glob(os.DirFS(root), pattern)
		if err != nil {
			t.Fatal(err)
		}
		for _, path := range paths {
			source := filepath.Join(root, path)
			info, err := os.Stat(source)
			if err != nil {
				t.Fatal(err)
			}
			if !info.Mode().IsRegular() {
				continue
			}
			content, err := os.ReadFile(source)
			if err != nil {
				t.Fatal(err)
			}
			dest := filepath.Join(buildDir, path)
			if err := os.MkdirAll(filepath.Dir(dest), 0755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(dest, content, 0644); err != nil {
				t.Fatal(err)
			}
		}
	}
	mock := runRecoveryContainer(t, ctx, testcontainers.ContainerRequest{
		FromDockerfile: testcontainers.FromDockerfile{
			Context: buildDir, Dockerfile: "python/perception/Dockerfile",
		},
		Env:          map[string]string{"INVISISCAN_PERCEPTION_LISTEN": "0.0.0.0:50051", "GRPC_MAX_MESSAGE_BYTES": "16777216"},
		ExposedPorts: []string{"50051/tcp"},
		WaitingFor:   wait.ForListeningPort("50051/tcp"),
	})
	target, err := mock.PortEndpoint(ctx, "50051/tcp", "")
	if err != nil {
		t.Fatal(err)
	}
	return dsn, pool, target
}

func runRecoveryContainer(t *testing.T, ctx context.Context, request testcontainers.ContainerRequest) testcontainers.Container {
	t.Helper()
	container, err := testcontainers.GenericContainer(ctx, testcontainers.GenericContainerRequest{
		ContainerRequest: request, Started: true,
	})
	testcontainers.CleanupContainer(t, container)
	if err != nil {
		t.Fatal(err)
	}
	return container
}

func recoveryCamera(t *testing.T, ctx context.Context, pool *pgxpool.Pool, name string) int64 {
	t.Helper()
	var id int64
	err := pool.QueryRow(ctx, `WITH location AS (INSERT INTO locations(name) VALUES($1) RETURNING id)
		INSERT INTO cameras(location_id,name,stream_url,publish_path,width,height,fps)
		SELECT id,$1,'rtsp://test/live',$1,1280,960,8 FROM location RETURNING id`, name).Scan(&id)
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func recoveryTrack(t *testing.T, ctx context.Context, s *store.Store, cameraID int64) store.ConfirmedTrackResult {
	t.Helper()
	started := time.Now().UTC().Truncate(time.Microsecond)
	result, err := s.CreateConfirmedTrack(ctx, store.ConfirmedTrack{
		CameraID: cameraID, EventUUID: uuid.NewString(), TrackUUID: uuid.NewString(),
		TrackerID: 1, StartedAt: started, ClockSource: "rtcp", Points: []store.Point{{Seq: 0, FrameTime: started}},
	})
	if err != nil {
		t.Fatal(err)
	}
	return result
}

type replayRows struct {
	events, tracks, points, ended int
}

func readReplayRows(t *testing.T, ctx context.Context, pool *pgxpool.Pool, cameraID int64) replayRows {
	t.Helper()
	var result replayRows
	err := pool.QueryRow(ctx, `SELECT (SELECT count(*) FROM events WHERE camera_id = $1),
		count(*), (SELECT count(*) FROM track_points p JOIN tracks t ON t.id = p.track_id WHERE t.camera_id = $1),
		count(*) FILTER (WHERE status = 'ended') FROM tracks WHERE camera_id = $1`, cameraID).
		Scan(&result.events, &result.tracks, &result.points, &result.ended)
	if err != nil {
		t.Fatal(err)
	}
	return result
}

func crashPipeline(t *testing.T, ctx context.Context, dsn string, pool *pgxpool.Pool, cmd *exec.Cmd,
	trigger string, cameraID int64, before replayRows) {
	t.Helper()
	gate, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer gate.Close(context.Background())
	const gateKey int64 = -150005
	if _, err := gate.Exec(ctx, "SELECT pg_advisory_lock($1)", gateKey); err != nil {
		t.Fatal(err)
	}
	// The backend pauses inside the real statement, after earlier writes in the
	// same transaction. This connection keeps it blocked until the child is dead.
	if _, err := pool.Exec(ctx, fmt.Sprintf(`CREATE FUNCTION pause_recovery_test() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN PERFORM pg_advisory_xact_lock(%d); RETURN NEW; END $$;
		CREATE TRIGGER pause_recovery_test %s EXECUTE FUNCTION pause_recovery_test();`, gateKey, trigger)); err != nil {
		t.Fatal(err)
	}

	var output bytes.Buffer
	cmd.Stdout, cmd.Stderr = &output, &output
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	var exitErr error
	go func() {
		exitErr = cmd.Wait()
		close(done)
	}()
	defer func() {
		_ = cmd.Process.Kill()
		<-done
	}()
	deadline, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	ticker := time.NewTicker(25 * time.Millisecond)
	defer ticker.Stop()
	waitForBackend := func(waiting bool) {
		t.Helper()
		for {
			var found bool
			check := "application_name = 'invisiscan-recovery-worker'"
			if waiting {
				check += " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
			}
			err := pool.QueryRow(deadline, "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE "+check+")").Scan(&found)
			if err != nil {
				t.Fatal(err)
			}
			if found == waiting {
				return
			}
			if waiting {
				select {
				case <-done:
					t.Fatalf("pipeline exited before reaching the crash point: %v\n%s", exitErr, output.String())
				default:
				}
			}
			select {
			case <-deadline.Done():
				t.Fatalf("worker backend did not reach expected state (waiting=%t): %v", waiting, deadline.Err())
			case <-ticker.C:
			}
		}
	}
	waitForBackend(true)
	if competitor, err := store.AcquireCamera(deadline, pool, cameraID); !errors.Is(err, store.ErrCameraOwned) {
		if competitor != nil {
			competitor.Close()
		}
		t.Fatalf("concurrent owner acquired camera or returned unexpected error: %v", err)
	}
	if got := readReplayRows(t, ctx, pool, cameraID); got != before {
		t.Fatalf("unexpected visible rows before crash: %+v, want %+v", got, before)
	}
	if err := cmd.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	<-done
	if exitErr == nil {
		t.Fatal("killed pipeline exited successfully")
	}
	waitForBackend(false)
	if got := readReplayRows(t, ctx, pool, cameraID); got != before {
		t.Fatalf("in-flight statement did not roll back: %+v, want %+v", got, before)
	}
	if _, err := pool.Exec(ctx, "DROP FUNCTION pause_recovery_test() CASCADE"); err != nil {
		t.Fatal(err)
	}
}
