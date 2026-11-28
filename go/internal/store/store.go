package store

import (
	"context"
	"errors"

	db "github.com/Squid2333/invisiscan-ai/go/internal/store/db"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var ErrNotFound = errors.New("store: row not found")

type database interface {
	db.DBTX
	Begin(context.Context) (pgx.Tx, error)
}

type Store struct {
	database database
	queries  *db.Queries
}

func New(pool *pgxpool.Pool) *Store {
	return newStore(pool)
}

func newStore(conn database) *Store {
	return &Store{database: conn, queries: db.New(conn)}
}
