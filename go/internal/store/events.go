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

type Event struct {
	ID           int64
	UUID         string
	CameraID     int64
	StartedAt    time.Time
	EndedAt      *time.Time
	Status       string
	ClosedReason *string
	TrackCount   int32
}

func (s *Store) CloseEvent(ctx context.Context, eventID int64, reason string) (bool, error) {
	result, err := s.queries.CloseEvent(ctx, db.CloseEventParams{
		ID: eventID, ClosedReason: pgtype.Text{String: reason, Valid: true},
	})
	if err != nil {
		return false, fmt.Errorf("close event: %w", err)
	}
	return result == 1, nil
}

func (s *Store) GetEvent(ctx context.Context, id int64) (Event, error) {
	result, err := s.queries.GetEvent(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return Event{}, ErrNotFound
	}
	if err != nil {
		return Event{}, fmt.Errorf("get event: %w", err)
	}
	row := Event{
		ID: result.ID, UUID: result.Uuid.String(), CameraID: result.CameraID,
		StartedAt: result.StartedAt.Time, Status: result.Status, TrackCount: result.TrackCount,
	}
	if result.EndedAt.Valid {
		row.EndedAt = &result.EndedAt.Time
	}
	if result.ClosedReason.Valid {
		row.ClosedReason = &result.ClosedReason.String
	}
	return row, nil
}
