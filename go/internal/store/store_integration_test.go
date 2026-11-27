package store

import (
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func testPool(t *testing.T, key string) *pgxpool.Pool {
	t.Helper()
	dsn := os.Getenv(key)
	if dsn == "" {
		t.Skipf("set %s to run PostgreSQL integration tests", key)
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("open %s: %v", key, err)
	}
	if err := pool.Ping(context.Background()); err != nil {
		pool.Close()
		t.Fatalf("ping %s: %v", key, err)
	}
	t.Cleanup(pool.Close)
	return pool
}

func TestPostgresStoreReadWrite(t *testing.T) {
	pool := testPool(t, "INVISISCAN_TEST_DSN")
	cloudPool := testPool(t, "INVISISCAN_CLOUD_TEST_DSN")
	s := New(pool)
	ctx := context.Background()
	fixture := newTestUUID()
	var locationID, cameraID int64
	if err := cloudPool.QueryRow(ctx, `INSERT INTO locations(name,description) VALUES($1,'store integration fixture') RETURNING id`, "store-test-"+fixture).Scan(&locationID); err != nil {
		t.Fatal(err)
	}
	cameraName := "store-test-" + fixture
	if err := cloudPool.QueryRow(ctx, `INSERT INTO cameras(location_id,name,stream_url,publish_path,width,height,fps) VALUES($1,$2,'rtsp://test/live',$3,640,480,8) RETURNING id`, locationID, cameraName, cameraName).Scan(&cameraID); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return exists(ctx, pool, "SELECT EXISTS(SELECT 1 FROM cameras WHERE id=$1)", cameraID) })
	base := time.Now().UTC().Truncate(time.Millisecond)
	rollbackEventUUID, rollbackTrackUUID := newTestUUID(), newTestUUID()
	if _, err := s.CreateConfirmedTrack(ctx, ConfirmedTrack{
		EventUUID: rollbackEventUUID, TrackUUID: rollbackTrackUUID, CameraID: cameraID, TrackerID: 270,
		StartedAt: base, ClockSource: "rtcp", Points: []Point{{Seq: -1, FrameTime: base}},
	}); err == nil {
		t.Fatal("expected invalid point to fail the confirmation transaction")
	}
	var rolledBack int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM events WHERE uuid=$1`, rollbackEventUUID).Scan(&rolledBack); err != nil || rolledBack != 0 {
		t.Fatalf("event from failed transaction remains: count=%d err=%v", rolledBack, err)
	}
	eventUUID, trackUUID := newTestUUID(), newTestUUID()
	score := float32(.91)
	created, err := s.CreateConfirmedTrack(ctx, ConfirmedTrack{
		EventUUID: eventUUID, TrackUUID: trackUUID, CameraID: cameraID, TrackerID: 271,
		StartedAt: base, ClockSource: "rtcp",
		Points: []Point{
			{Seq: 0, FrameTime: base, BBoxX: 11, BBoxY: 12, BBoxW: 40, BBoxH: 90, Depth: 1.25, DetScore: &score},
			{Seq: 1, FrameTime: base.Add(125 * time.Millisecond), BBoxX: 13, BBoxY: 14, BBoxW: 41, BBoxH: 91, Depth: 1.2},
		},
	})
	if err != nil {
		t.Fatalf("create confirmed track: %v", err)
	}
	event, err := s.GetEvent(ctx, created.EventID)
	if err != nil {
		t.Fatal(err)
	}
	if event.Status != "open" || event.TrackCount != 1 || event.CameraID != cameraID {
		t.Fatalf("unexpected event: %+v", event)
	}
	track, err := s.GetTrack(ctx, created.TrackID)
	if err != nil {
		t.Fatal(err)
	}
	if track.EventID != event.ID || track.Status != "confirmed" || track.FrameCount != 2 {
		t.Fatalf("unexpected track: %+v", track)
	}
	points, err := s.ListTrackPoints(ctx, track.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(points) != 2 || points[1].Seq != 1 || points[0].BBoxX != 11 {
		t.Fatalf("unexpected points: %+v", points)
	}

	if err := s.InsertTrackImages(ctx, track.ID, []TrackImage{{Kind: "body", Seq: 0, ObjectKey: "integration/" + trackUUID + "/body-0.jpg", Selected: true}}); err != nil {
		t.Fatal(err)
	}
	images, err := s.ListTrackImages(ctx, track.ID)
	if err != nil || len(images) != 1 || images[0].ObjectKey != "integration/"+trackUUID+"/body-0.jpg" {
		t.Fatalf("images=%+v err=%v", images, err)
	}

	log := RecognitionLog{UUID: newTestUUID(), TrackID: track.ID, EventID: event.ID, CameraID: cameraID, LocationID: locationID, Modality: "face", OccurredAt: base, Action: "enter", Score: 0, Rounds: 1, Decision: "no_face"}
	logID, inserted, err := s.InsertRecognitionLog(ctx, log)
	if err != nil || !inserted {
		t.Fatalf("insert recognition log: id=%d inserted=%v err=%v", logID, inserted, err)
	}
	if _, inserted, err = s.InsertRecognitionLog(ctx, log); err != nil || inserted {
		t.Fatalf("duplicate log should be ignored: inserted=%v err=%v", inserted, err)
	}
	gotLog, err := s.GetRecognitionLog(ctx, track.ID, "face")
	if err != nil || gotLog.TrackID != track.ID || gotLog.Decision != "no_face" {
		t.Fatalf("log=%+v err=%v", gotLog, err)
	}

	updated, err := s.EndTrack(ctx, track.ID, "confirmed", "lost", base.Add(time.Second))
	if err != nil || !updated {
		t.Fatalf("end track: updated=%v err=%v", updated, err)
	}
	updated, err = s.EndTrack(ctx, track.ID, "confirmed", "lost", base.Add(2*time.Second))
	if err != nil || updated {
		t.Fatalf("conditional update repeated: updated=%v err=%v", updated, err)
	}
	closed, err := s.CloseEvent(ctx, event.ID, "absence")
	if err != nil || !closed {
		t.Fatalf("close event: closed=%v err=%v", closed, err)
	}
	closed, err = s.CloseEvent(ctx, event.ID, "absence")
	if err != nil || closed {
		t.Fatalf("conditional event close repeated: closed=%v err=%v", closed, err)
	}
}

func TestPostgresBidirectionalReplication(t *testing.T) {
	if os.Getenv("INVISISCAN_REPLICATION_TEST") != "1" {
		t.Skip("set INVISISCAN_REPLICATION_TEST=1 to run two-site replication checks")
	}
	aPool := testPool(t, "INVISISCAN_A_TEST_DSN")
	cloudPool := testPool(t, "INVISISCAN_CLOUD_TEST_DSN")
	ctx := context.Background()

	for _, check := range []struct {
		pool *pgxpool.Pool
		sub  string
	}{{aPool, "sub_cloud"}, {cloudPool, "sub_a"}} {
		var origin string
		if err := check.pool.QueryRow(ctx, "SELECT suborigin FROM pg_subscription WHERE subname=$1", check.sub).Scan(&origin); err != nil {
			t.Fatalf("subscription %s: %v", check.sub, err)
		}
		if origin != "none" {
			t.Fatalf("subscription %s origin=%q", check.sub, origin)
		}
	}
	for _, table := range []string{"events", "tracks", "track_points", "track_images", "recognition_logs", "recordings", "domain_events", "pipeline_heartbeats", "face_embeddings", "subjects", "face_templates"} {
		assertPublicationTable(t, aPool, "pub_a", table)
	}
	for _, table := range []string{"locations", "cameras", "attendance_rules", "subjects", "face_templates"} {
		assertPublicationTable(t, cloudPool, "pub_cloud", table)
	}
	for _, table := range []string{"locations", "cameras", "attendance_rules"} {
		assertNotPublicationTable(t, aPool, "pub_a", table)
	}
	for _, table := range []string{"events", "tracks", "track_points", "track_images", "recognition_logs", "recordings", "domain_events", "pipeline_heartbeats", "face_embeddings"} {
		assertNotPublicationTable(t, cloudPool, "pub_cloud", table)
	}
	locUUID := newTestUUID()
	locationName := "replication-test-" + locUUID
	var locationID int64
	if err := cloudPool.QueryRow(ctx, `INSERT INTO locations(name,description) VALUES($1,$2) RETURNING id`, locationName, "integration fixture").Scan(&locationID); err != nil {
		t.Fatal(err)
	}
	camName := "replication-test-" + newTestUUID()
	if _, err := cloudPool.Exec(ctx, `INSERT INTO cameras(location_id,name,stream_url,publish_path,width,height,fps) VALUES($1,$2,'rtsp://test/live',$3,640,480,8)`, locationID, camName, camName); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return exists(ctx, aPool, "SELECT EXISTS(SELECT 1 FROM cameras WHERE name=$1)", camName) })
	var cameraID int64
	if err := aPool.QueryRow(ctx, "SELECT id FROM cameras WHERE name=$1", camName).Scan(&cameraID); err != nil {
		t.Fatal(err)
	}

	// The A-owned event aggregate and its dependent rows arrive at cloud.
	aStore := New(aPool)
	base := time.Now().UTC().Truncate(time.Millisecond)
	eventUUID, trackUUID := newTestUUID(), newTestUUID()
	trackResult, err := aStore.CreateConfirmedTrack(ctx, ConfirmedTrack{
		EventUUID: eventUUID, TrackUUID: trackUUID, CameraID: cameraID, TrackerID: 810,
		StartedAt: base, ClockSource: "rtcp", Points: []Point{{Seq: 0, FrameTime: base, BBoxX: 1, BBoxY: 2, BBoxW: 30, BBoxH: 60, Depth: .8}},
	})
	if err != nil {
		t.Fatal(err)
	}
	imageKey := "integration/" + trackUUID + "/body.jpg"
	if err := aStore.InsertTrackImages(ctx, trackResult.TrackID, []TrackImage{{Kind: "body", Seq: 0, ObjectKey: imageKey, Selected: true}}); err != nil {
		t.Fatal(err)
	}
	logUUID := newTestUUID()
	if _, inserted, err := aStore.InsertRecognitionLog(ctx, RecognitionLog{UUID: logUUID, TrackID: trackResult.TrackID, EventID: trackResult.EventID, CameraID: cameraID, LocationID: locationID, Modality: "face", OccurredAt: base, Action: "enter", Rounds: 1, Decision: "no_face"}); err != nil || !inserted {
		t.Fatalf("insert replicated recognition log: inserted=%v err=%v", inserted, err)
	}
	waitFor(t, func() bool {
		return exists(ctx, cloudPool, "SELECT EXISTS(SELECT 1 FROM events WHERE uuid=$1)", eventUUID) &&
			exists(ctx, cloudPool, "SELECT EXISTS(SELECT 1 FROM tracks WHERE uuid=$1)", trackUUID) &&
			exists(ctx, cloudPool, "SELECT EXISTS(SELECT 1 FROM track_points p JOIN tracks t ON t.id=p.track_id WHERE t.uuid=$1 AND p.seq=0)", trackUUID) &&
			exists(ctx, cloudPool, "SELECT EXISTS(SELECT 1 FROM track_images i JOIN tracks t ON t.id=i.track_id WHERE t.uuid=$1 AND i.object_key=$2)", trackUUID, imageKey) &&
			exists(ctx, cloudPool, "SELECT EXISTS(SELECT 1 FROM recognition_logs WHERE uuid=$1)", logUUID)
	})
	if _, err := aStore.EndTrack(ctx, trackResult.TrackID, "confirmed", "lost", base.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if _, err := aStore.CloseEvent(ctx, trackResult.EventID, "absence"); err != nil {
		t.Fatal(err)
	}

	// Both sites insert into subjects. Odd/even identity ranges prevent ID collisions;
	// origin=none prevents the subscriber from echoing the row back.
	aUUID, cloudUUID := newTestUUID(), newTestUUID()
	var aSubjectID, cloudSubjectID int64
	if err := aPool.QueryRow(ctx, `INSERT INTO subjects(uuid,kind) VALUES($1,'unknown') RETURNING id`, aUUID).Scan(&aSubjectID); err != nil {
		t.Fatal(err)
	}
	if err := cloudPool.QueryRow(ctx, `INSERT INTO subjects(uuid,kind) VALUES($1,'unknown') RETURNING id`, cloudUUID).Scan(&cloudSubjectID); err != nil {
		t.Fatal(err)
	}
	if aSubjectID%2 != 1 || cloudSubjectID%2 != 0 {
		t.Fatalf("site ID ranges: A=%d cloud=%d", aSubjectID, cloudSubjectID)
	}
	waitFor(t, func() bool {
		return exists(ctx, cloudPool, "SELECT EXISTS(SELECT 1 FROM subjects WHERE uuid=$1)", aUUID) && exists(ctx, aPool, "SELECT EXISTS(SELECT 1 FROM subjects WHERE uuid=$1)", cloudUUID)
	})
	if countUUID(ctx, aPool, "subjects", aUUID) != 1 || countUUID(ctx, cloudPool, "subjects", aUUID) != 1 {
		t.Fatal("A subject was not replicated exactly once")
	}
	aTemplateUUID, cloudTemplateUUID := newTestUUID(), newTestUUID()
	var aTemplateID, cloudTemplateID int64
	if err := aPool.QueryRow(ctx, `INSERT INTO face_templates(uuid,subject_id,object_key,source) VALUES($1,$2,$3,'auto_enroll') RETURNING id`, aTemplateUUID, aSubjectID, "integration/"+aTemplateUUID+".jpg").Scan(&aTemplateID); err != nil {
		t.Fatal(err)
	}
	if err := cloudPool.QueryRow(ctx, `INSERT INTO face_templates(uuid,subject_id,object_key,source) VALUES($1,$2,$3,'manual') RETURNING id`, cloudTemplateUUID, cloudSubjectID, "integration/"+cloudTemplateUUID+".jpg").Scan(&cloudTemplateID); err != nil {
		t.Fatal(err)
	}
	if aTemplateID%2 != 1 || cloudTemplateID%2 != 0 {
		t.Fatalf("template ID ranges: A=%d cloud=%d", aTemplateID, cloudTemplateID)
	}
	waitFor(t, func() bool {
		return exists(ctx, cloudPool, "SELECT EXISTS(SELECT 1 FROM face_templates WHERE uuid=$1)", aTemplateUUID) && exists(ctx, aPool, "SELECT EXISTS(SELECT 1 FROM face_templates WHERE uuid=$1)", cloudTemplateUUID)
	})
	if countUUID(ctx, aPool, "face_templates", aTemplateUUID) != 1 || countUUID(ctx, cloudPool, "face_templates", aTemplateUUID) != 1 {
		t.Fatal("A template was not replicated exactly once")
	}

	// Cloud owns camera configuration; A receives it. A-owned domain events catch up
	// after the cloud subscription is disabled and re-enabled to simulate a link outage.
	if _, err := cloudPool.Exec(ctx, "ALTER SUBSCRIPTION sub_a DISABLE"); err != nil {
		t.Fatalf("disable A subscription: %v", err)
	}
	eventType := "integration.replication." + newTestUUID()
	if _, err := aPool.Exec(ctx, `INSERT INTO domain_events(type,payload) VALUES($1,'{}'::jsonb)`, eventType); err != nil {
		t.Fatal(err)
	}
	if _, err := cloudPool.Exec(ctx, "ALTER SUBSCRIPTION sub_a ENABLE"); err != nil {
		t.Fatalf("enable A subscription: %v", err)
	}
	waitFor(t, func() bool {
		return exists(ctx, cloudPool, "SELECT EXISTS(SELECT 1 FROM domain_events WHERE type=$1)", eventType)
	})
}

func assertPublicationTable(t *testing.T, pool *pgxpool.Pool, pub, table string) {
	t.Helper()
	if !exists(context.Background(), pool, "SELECT EXISTS(SELECT 1 FROM pg_publication_tables WHERE pubname=$1 AND tablename=$2)", pub, table) {
		t.Fatalf("%s does not publish %s", pub, table)
	}
}
func assertNotPublicationTable(t *testing.T, pool *pgxpool.Pool, pub, table string) {
	t.Helper()
	if exists(context.Background(), pool, "SELECT EXISTS(SELECT 1 FROM pg_publication_tables WHERE pubname=$1 AND tablename=$2)", pub, table) {
		t.Fatalf("%s unexpectedly publishes %s", pub, table)
	}
}
func exists(ctx context.Context, pool *pgxpool.Pool, query string, args ...any) bool {
	var value bool
	return pool.QueryRow(ctx, query, args...).Scan(&value) == nil && value
}
func countUUID(ctx context.Context, pool *pgxpool.Pool, table, uuid string) int {
	var n int
	_ = pool.QueryRow(ctx, fmt.Sprintf("SELECT count(*) FROM %s WHERE uuid=$1", pgx.Identifier{table}.Sanitize()), uuid).Scan(&n)
	return n
}
func waitFor(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
	t.Fatal("timed out waiting for logical replication")
}
func newTestUUID() string {
	return fmt.Sprintf("%08x-%04x-4%03x-8%03x-%012x", time.Now().UnixNano()>>16&0xffffffff, time.Now().UnixNano()>>8&0xffff, time.Now().UnixNano()&0xfff, time.Now().UnixNano()>>12&0xfff, time.Now().UnixNano()&0xffffffffffff)
}
