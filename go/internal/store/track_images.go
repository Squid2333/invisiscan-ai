package store

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5"
)

type TrackImage struct {
	Kind      string
	Seq       int32
	ObjectKey string
	Quality   *float32
	Selected  bool
}

func (s *Store) InsertTrackImages(ctx context.Context, trackID int64, images []TrackImage) error {
	if len(images) == 0 {
		return nil
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin track images: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	rows := make([][]any, 0, len(images))
	for _, image := range images {
		rows = append(rows, []any{trackID, image.Kind, image.Seq, image.ObjectKey, image.Quality, image.Selected})
	}
	if _, err = tx.CopyFrom(ctx, pgx.Identifier{"track_images"}, []string{
		"track_id", "kind", "seq", "object_key", "quality", "selected",
	}, pgx.CopyFromRows(rows)); err != nil {
		return fmt.Errorf("copy track images: %w", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit track images: %w", err)
	}
	return nil
}

func (s *Store) ListTrackImages(ctx context.Context, trackID int64) ([]TrackImage, error) {
	rows, err := s.queries.ListTrackImages(ctx, trackID)
	if err != nil {
		return nil, fmt.Errorf("list track images: %w", err)
	}
	images := make([]TrackImage, 0, len(rows))
	for _, row := range rows {
		image := TrackImage{Kind: row.Kind, Seq: row.Seq, ObjectKey: row.ObjectKey, Selected: row.Selected}
		if row.Quality.Valid {
			quality := row.Quality.Float32
			image.Quality = &quality
		}
		images = append(images, image)
	}
	return images, nil
}
