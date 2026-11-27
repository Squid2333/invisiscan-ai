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

type RecognitionLog struct {
	UUID              string
	TrackID           int64
	EventID           int64
	CameraID          int64
	LocationID        int64
	SubjectID         *int64
	MatchedTemplateID *int64
	Modality          string
	OccurredAt        time.Time
	Action            string
	Score             float32
	Rounds            int16
	Decision          string
}

func (s *Store) InsertRecognitionLog(ctx context.Context, in RecognitionLog) (int64, bool, error) {
	var uuid pgtype.UUID
	if err := uuid.Scan(in.UUID); err != nil {
		return 0, false, fmt.Errorf("parse recognition log uuid: %w", err)
	}
	var subjectID, templateID pgtype.Int8
	if in.SubjectID != nil {
		subjectID = pgtype.Int8{Int64: *in.SubjectID, Valid: true}
	}
	if in.MatchedTemplateID != nil {
		templateID = pgtype.Int8{Int64: *in.MatchedTemplateID, Valid: true}
	}
	id, err := s.queries.InsertRecognitionLog(ctx, db.InsertRecognitionLogParams{
		Uuid: uuid, TrackID: in.TrackID, EventID: in.EventID, CameraID: in.CameraID, LocationID: in.LocationID,
		SubjectID: subjectID, MatchedTemplateID: templateID, Modality: in.Modality,
		OccurredAt: pgtype.Timestamptz{Time: in.OccurredAt, Valid: true}, Action: in.Action,
		Score: in.Score, Rounds: in.Rounds, Decision: in.Decision,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, fmt.Errorf("insert recognition log: %w", err)
	}
	return id, true, nil
}

func (s *Store) GetRecognitionLog(ctx context.Context, trackID int64, modality string) (RecognitionLog, error) {
	result, err := s.queries.GetRecognitionLog(ctx, db.GetRecognitionLogParams{TrackID: trackID, Modality: modality})
	if errors.Is(err, pgx.ErrNoRows) {
		return RecognitionLog{}, ErrNotFound
	}
	if err != nil {
		return RecognitionLog{}, fmt.Errorf("get recognition log: %w", err)
	}
	row := RecognitionLog{
		UUID: result.Uuid.String(), TrackID: result.TrackID, EventID: result.EventID,
		CameraID: result.CameraID, LocationID: result.LocationID, Modality: result.Modality,
		OccurredAt: result.OccurredAt.Time, Action: result.Action, Score: result.Score,
		Rounds: result.Rounds, Decision: result.Decision,
	}
	if result.SubjectID.Valid {
		subjectID := result.SubjectID.Int64
		row.SubjectID = &subjectID
	}
	if result.MatchedTemplateID.Valid {
		templateID := result.MatchedTemplateID.Int64
		row.MatchedTemplateID = &templateID
	}
	return row, nil
}
