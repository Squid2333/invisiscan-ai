package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/Squid2333/invisiscan-ai/gen/go/invisiscan/v1"
	"github.com/Squid2333/invisiscan-ai/go/internal/config"
	"github.com/Squid2333/invisiscan-ai/go/internal/pipeline"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func main() {
	configPath := flag.String("config", "config/dev.yaml", "path to pipeline YAML config")
	flag.Parse()

	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(*configPath, logger); err != nil {
		logger.Error("pipeline stopped", "error", err)
		os.Exit(1)
	}
}

func run(configPath string, logger *slog.Logger) error {
	cfg, err := config.Load(configPath)
	if err != nil {
		return fmt.Errorf("load pipeline config: %w", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	startupCtx, cancelStartup := context.WithTimeout(ctx, cfg.StartupTimeout)
	defer cancelStartup()

	dbConfig, err := pgxpool.ParseConfig(cfg.DatabaseURL)
	if err != nil {
		return fmt.Errorf("parse database URL: %w", err)
	}
	db, err := pgxpool.NewWithConfig(startupCtx, dbConfig)
	if err != nil {
		return fmt.Errorf("create database pool: %w", err)
	}
	defer db.Close()
	if err := db.Ping(startupCtx); err != nil {
		return fmt.Errorf("ping database: %w", err)
	}

	conn, err := grpc.NewClient(
		cfg.PerceptionTarget,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithDefaultCallOptions(
			grpc.MaxCallRecvMsgSize(cfg.MaxMessageBytes),
			grpc.MaxCallSendMsgSize(cfg.MaxMessageBytes),
		),
	)
	if err != nil {
		return fmt.Errorf("create perception connection: %w", err)
	}
	defer conn.Close()

	client := invisiscanv1.NewPerceptionServiceClient(conn)
	return pipeline.RunMock(ctx, startupCtx, cfg, db, client, logger)
}
