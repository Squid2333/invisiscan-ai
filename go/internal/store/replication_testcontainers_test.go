package store

import (
	"context"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/network"
	"github.com/testcontainers/testcontainers-go/wait"
)

type replicationSite struct {
	container testcontainers.Container
	pool      *pgxpool.Pool
}

const subscriptionFile = "/tmp/invisiscan-subscription.sql"

func startReplicationSites(t *testing.T, ctx context.Context) (*pgxpool.Pool, *pgxpool.Pool) {
	t.Helper()
	migrations, err := filepath.Abs("../../../migrations")
	if err != nil {
		t.Fatal(err)
	}
	siteNetwork, err := network.New(ctx)
	if err != nil {
		t.Fatal(err)
	}
	testcontainers.CleanupNetwork(t, siteNetwork)
	a := startReplicationSite(t, ctx, migrations, siteNetwork.Name, "a")
	cloud := startReplicationSite(t, ctx, migrations, siteNetwork.Name, "cloud")

	// 每个订阅都在另一台 PostgreSQL 实例上创建复制槽。
	// 沿用部署迁移的顺序，先创建云端订阅。
	runSubscription(t, ctx, cloud.container, "a_conninfo=host=a dbname=invisiscan user=invisiscan_repl password=integration")
	runSubscription(t, ctx, a.container, "cloud_conninfo=host=cloud dbname=invisiscan user=invisiscan_repl password=integration")
	for _, site := range []replicationSite{a, cloud} {
		waitFor(t, func() bool {
			return exists(ctx, site.pool, "SELECT count(*) > 0 AND bool_and(srsubstate = 'r') FROM pg_subscription_rel")
		})
	}
	waitFor(t, func() bool {
		return exists(ctx, a.pool, "SELECT EXISTS(SELECT 1 FROM cameras WHERE id = 1)")
	})
	return a.pool, cloud.pool
}

func startReplicationSite(t *testing.T, ctx context.Context, migrations, networkName, site string) replicationSite {
	t.Helper()
	container, err := testcontainers.GenericContainer(ctx, testcontainers.GenericContainerRequest{
		Started: true,
		ContainerRequest: testcontainers.ContainerRequest{
			Image: "pgvector/pgvector:pg16",
			Env: map[string]string{
				"POSTGRES_DB": "invisiscan", "POSTGRES_USER": "invisiscan", "POSTGRES_PASSWORD": "invisiscan",
			},
			Cmd:            []string{"postgres", "-c", "wal_level=logical"},
			ExposedPorts:   []string{"5432/tcp"},
			Networks:       []string{networkName},
			NetworkAliases: map[string][]string{networkName: {site}},
			Files: []testcontainers.ContainerFile{{
				HostFilePath:      filepath.Join(migrations, "site", site, "30_subscription.sql"),
				ContainerFilePath: subscriptionFile, FileMode: 0644,
			}},
			WaitingFor: wait.ForLog("database system is ready to accept connections").WithOccurrence(2),
		},
	})
	testcontainers.CleanupContainer(t, container)
	if err != nil {
		t.Fatal(err)
	}
	endpoint, err := container.PortEndpoint(ctx, "5432/tcp", "")
	if err != nil {
		t.Fatal(err)
	}
	dsn := url.URL{Scheme: "postgres", User: url.UserPassword("invisiscan", "invisiscan"),
		Host: endpoint, Path: "/invisiscan", RawQuery: "sslmode=disable"}
	pool, err := pgxpool.New(ctx, dsn.String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	scripts := []string{"0001_init.sql"}
	if site == "cloud" {
		scripts = append(scripts, "0002_seed.sql")
	}
	scripts = append(scripts, "0003_contract_checks.sql", filepath.Join("site", site, "10_id_sequences.sql"),
		filepath.Join("site", site, "20_publication.sql"))
	for _, script := range scripts {
		sql, err := os.ReadFile(filepath.Join(migrations, script))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := pool.Exec(ctx, string(sql)); err != nil {
			t.Fatalf("apply %s on %s: %v", script, site, err)
		}
	}
	if _, err := pool.Exec(ctx, "ALTER ROLE invisiscan_repl PASSWORD 'integration'"); err != nil {
		t.Fatal(err)
	}
	return replicationSite{container: container, pool: pool}
}

func runSubscription(t *testing.T, ctx context.Context, container testcontainers.Container, conninfo string) {
	t.Helper()
	code, output, err := container.Exec(ctx, []string{"psql", "-X", "-U", "invisiscan", "-d", "invisiscan",
		"-v", "ON_ERROR_STOP=1", "-v", conninfo, "-f", subscriptionFile})
	if err != nil {
		t.Fatal(err)
	}
	if code != 0 {
		log, err := io.ReadAll(output)
		t.Fatalf("create subscription (exit %d): %s (%v)", code, log, err)
	}
}
