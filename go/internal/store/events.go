package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	db "github.com/Squid2333/invisiscan-ai/go/internal/store/db"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

// EventExists 查这个事件 UUID 是不是已经在库里了。
// 主要是给假回放用的：同一段回放会算出同一个 UUID，重启后先问一句“这次写过了吗”，
// 写过了就不再重复记一遍。
func (s *Store) EventExists(ctx context.Context, id uuid.UUID) (bool, error) {
	found, err := s.queries.EventExists(ctx, pgtype.UUID{Bytes: id, Valid: true})
	if err != nil {
		return false, fmt.Errorf("check event UUID: %w", err)
	}
	return found, nil
}

// Event 是数据库里的一条“事件”：某个相机在某一小段时间里有人经过。
// 一条事件下面挂零到多条轨迹，TrackCount 就是挂了几条。
// EndedAt 和 ClosedReason 用指针：事件还开着的时候它们是 nil，直接判空就知道“还没结束”。
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

// CloseEvent 把一条还开着的事件关掉：写状态、写关闭原因、写结束时间。
// 结束时间不是“关的这一刻”，而是这条事件下面所有轨迹里最晚的那个结束时间；
// 一条轨迹都没有时就用事件自己的开始时间。这样事件的时间范围始终对着实际数据，不会凭空拉长。
//
// 只对 status = 'open' 的事件生效：已经关过的事件再关一次不会改动任何东西。
// 返回 true 表示这次真的关掉了；false 表示它本来就不是开着的（多半是别处已经关过了）。
func (s *Store) CloseEvent(ctx context.Context, eventID int64, reason string) (bool, error) {
	result, err := s.queries.CloseEvent(ctx, db.CloseEventParams{
		ID: eventID, ClosedReason: pgtype.Text{String: reason, Valid: true},
	})
	if err != nil {
		return false, fmt.Errorf("close event: %w", err)
	}
	return result == 1, nil
}

// GetEvent 按 ID 读一条事件，主要给测试和排查用。
// 查不到就返回 ErrNotFound，和这个包其他方法一个说法。
// 库里是 NULL 的字段（结束时间、关闭原因）读出来给 nil，调用方直接判空就行。
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
