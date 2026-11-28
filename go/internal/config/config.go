package config

import (
	"fmt"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/knadh/koanf/parsers/yaml"
	"github.com/knadh/koanf/providers/env"
	"github.com/knadh/koanf/providers/file"
	"github.com/knadh/koanf/v2"
)

type Config struct {
	InstanceID       string        `koanf:"instance_id"`
	DatabaseURL      string        `koanf:"database_url"`
	PerceptionTarget string        `koanf:"perception_target"`
	MaxMessageBytes  int           `koanf:"max_message_bytes"`
	StartupTimeout   time.Duration `koanf:"startup_timeout"`
	MockCameraID     int64         `koanf:"mock_camera_id"`
	MockReplayID     string        `koanf:"mock_replay_id"`
	MinTrackFrames   int32         `koanf:"min_track_frames"`
	MaxAbsentFrames  int           `koanf:"max_absent_frames"`
	DepthEMAAlpha    float64       `koanf:"depth_ema_alpha"`
}

func Load(path string) (Config, error) {
	k := koanf.New(".")
	if path != "" {
		if _, err := os.Stat(path); err == nil {
			if err := k.Load(file.Provider(path), yaml.Parser()); err != nil {
				return Config{}, fmt.Errorf("load config file: %w", err)
			}
		} else if !os.IsNotExist(err) {
			return Config{}, fmt.Errorf("stat config file: %w", err)
		}
	}

	if err := k.Load(env.Provider("INVISISCAN_", ".", func(key string) string {
		return strings.ToLower(strings.TrimPrefix(key, "INVISISCAN_"))
	}), nil); err != nil {
		return Config{}, fmt.Errorf("load environment: %w", err)
	}

	// 帧数取自 legacy/RGBDSort/sort.py。深度平滑系数取自
	// legacy/RGBDSort/RGBsort.py 的 update_track_depth_cache。
	cfg := Config{MockCameraID: 1, MockReplayID: "default", MinTrackFrames: 3, MaxAbsentFrames: 20, DepthEMAAlpha: 0.8}
	if err := k.Unmarshal("", &cfg); err != nil {
		return Config{}, fmt.Errorf("decode config: %w", err)
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func (c Config) Validate() error {
	if c.InstanceID == "" {
		return fmt.Errorf("instance_id is required")
	}
	parsed, err := url.Parse(c.DatabaseURL)
	if err != nil || parsed.Scheme != "postgres" && parsed.Scheme != "postgresql" || parsed.Host == "" {
		return fmt.Errorf("database_url must be a PostgreSQL URL")
	}
	if c.PerceptionTarget == "" {
		return fmt.Errorf("perception_target is required")
	}
	if c.MaxMessageBytes < 1<<20 {
		return fmt.Errorf("max_message_bytes must be at least 1048576")
	}
	if c.StartupTimeout <= 0 {
		return fmt.Errorf("startup_timeout must be positive")
	}
	if c.MockCameraID <= 0 || c.MinTrackFrames <= 0 || c.MaxAbsentFrames <= 0 {
		return fmt.Errorf("mock_camera_id, min_track_frames and max_absent_frames must be positive")
	}
	if strings.TrimSpace(c.MockReplayID) == "" {
		return fmt.Errorf("mock_replay_id must not be empty")
	}
	if !(c.DepthEMAAlpha >= 0 && c.DepthEMAAlpha <= 1) {
		return fmt.Errorf("depth_ema_alpha must be between 0 and 1")
	}
	return nil
}
