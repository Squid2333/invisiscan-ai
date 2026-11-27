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

type ConfirmedTrack struct {
	EventUUID   string
	TrackUUID   string
	CameraID    int64
	TrackerID   int32
	StartedAt   time.Time
	ClockSource string
	Points      []Point
}

type ConfirmedTrackResult struct {
	EventID int64
	TrackID int64
}

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

func (s *Store) CreateConfirmedTrack(ctx context.Context, in ConfirmedTrack) (out ConfirmedTrackResult, err error) {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return out, fmt.Errorf("begin confirmed track: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	queries := s.queries.WithTx(tx)

	// Locking the camera serializes event creation for tracks from the same stream.
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
		rows := make([][]any, 0, len(in.Points))
		for _, point := range in.Points {
			rows = append(rows, []any{out.TrackID, point.Seq, point.FrameTime, point.BBoxX, point.BBoxY, point.BBoxW, point.BBoxH, point.Depth, point.DetScore})
		}
		if _, err = tx.CopyFrom(ctx, pgx.Identifier{"track_points"}, []string{
			"track_id", "seq", "frame_time", "bbox_x", "bbox_y", "bbox_w", "bbox_h", "depth", "det_score",
		}, pgx.CopyFromRows(rows)); err != nil {
			return out, fmt.Errorf("copy track points: %w", err)
		}
	}
	if err = tx.Commit(ctx); err != nil {
		return out, fmt.Errorf("commit confirmed track: %w", err)
	}
	out.EventID = eventID
	return out, nil
}

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
