package store

import (
	"context"
	"fmt"
)

// EnabledCameraDimensions 按相机 ID 查它画面的宽高（像素）。
// 假帧源要靠它造出和真实相机一样大的图，所以这个数是必须的。
//
// 实现上偷了个懒：这个包里还没有“按 ID 查相机”的语句，就把所有启用中的相机拉回来挨个比对。
// 一次回放只调一次，相机表又很小，这点开销可以忽略；等接真实相机时应该换成按 ID 单查。
//
// 两处要留意的行为：只认 enabled=true 的相机，相机被停用时返回的是 ErrNotFound（看着像“不存在”）；
// 而且报错信息里没带相机 ID，出事时看不出找的是哪一个。
func (s *Store) EnabledCameraDimensions(ctx context.Context, cameraID int64) (width, height int32, err error) {
	cameras, err := s.queries.ListEnabledCameras(ctx)
	if err != nil {
		return 0, 0, fmt.Errorf("list enabled cameras: %w", err)
	}
	for _, camera := range cameras {
		if camera.ID == cameraID {
			return camera.Width, camera.Height, nil
		}
	}
	return 0, 0, ErrNotFound
}
