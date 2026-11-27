package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/Squid2333/invisiscan-ai/gen/go/invisiscan/v1"
	"github.com/Squid2333/invisiscan-ai/go/internal/config"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/timestamppb"
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

	if err := checkMockPerception(startupCtx, conn, cfg); err != nil {
		return fmt.Errorf("check perception service: %w", err)
	}
	logger.Info("pipeline skeleton ready",
		"instance_id", cfg.InstanceID,
		"perception_target", cfg.PerceptionTarget,
	)
	<-ctx.Done()
	logger.Info("pipeline shutdown complete")
	return nil
}

func checkMockPerception(ctx context.Context, conn *grpc.ClientConn, cfg config.Config) error {
	client := invisiscanv1.NewPerceptionServiceClient(conn)
	stream, err := client.Perceive(ctx)
	if err != nil {
		return err
	}
	if err := stream.Send(&invisiscanv1.PerceiveRequest{Payload: &invisiscanv1.PerceiveRequest_Config{
		Config: &invisiscanv1.SessionConfig{StreamId: cfg.InstanceID},
	}}); err != nil {
		return err
	}
	response, err := stream.Recv()
	if err != nil {
		return err
	}
	if response.GetReady() == nil {
		return fmt.Errorf("first response was not SessionReady")
	}
	if err := stream.Send(&invisiscanv1.PerceiveRequest{Payload: &invisiscanv1.PerceiveRequest_Frame{
		Frame: &invisiscanv1.FrameRequest{
			FrameSeq:  1,
			FrameTime: timestamppb.New(time.Now().UTC()),
			Image: &invisiscanv1.Image{
				Encoding: invisiscanv1.Image_ENCODING_RAW_BGR24,
				Data:     []byte{0, 0, 0},
				Width:    1,
				Height:   1,
			},
		},
	}}); err != nil {
		return err
	}
	response, err = stream.Recv()
	if err != nil {
		return err
	}
	if result := response.GetResult(); result == nil || result.FrameSeq != 1 {
		return fmt.Errorf("mock PerceptionService returned an unexpected FrameResult")
	}
	return stream.CloseSend()
}
