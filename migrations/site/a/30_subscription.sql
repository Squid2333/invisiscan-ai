-- site/a/30_subscription.sql: host A only. Subscribes to the cloud's pub_cloud.
--
-- Setup order (both sites already ran 0001_init.sql, 10_id_sequences.sql and
-- 20_publication.sql; the cloud also ran 0002_seed.sql):
--   1. Cloud: site/cloud/30_subscription.sql (copy_data = false). Its slot on
--      host A starts recording before host A has written anything.
--   2. Host A: this file (copy_data = true). The initial sync brings the seed
--      data and anything invisiscan-main has already written.
--   3. Wait until every table is synced, then start the pipeline:
--        SELECT srrelid::regclass, srsubstate FROM pg_subscription_rel;  -- all 'r'
--
-- The rows copied in step 2 are never sent back to the cloud: the cloud's
-- subscription does not copy existing data, and origin = none drops changes
-- that host A applied from the cloud.
--
-- CREATE SUBSCRIPTION prints a WARNING that copy_data with origin = none
-- "might copy data that had a different origin", because subjects and
-- face_templates on the cloud are also written by sub_a. It is harmless here:
-- host A has written nothing yet, so the cloud holds no rows from host A.
--
-- The connection string goes in a psql variable; keep the password in the
-- postgres user's ~/.pgpass on host A, not here:
--   psql -d invisiscan -v cloud_conninfo='host=10.8.0.1 port=5432 dbname=invisiscan user=invisiscan_repl' \
--        -f 30_subscription.sql

CREATE SUBSCRIPTION sub_cloud
    CONNECTION :'cloud_conninfo'
    PUBLICATION pub_cloud
    WITH (
        origin    = none,  -- do not receive changes the cloud applied from host A
        copy_data = true,
        streaming = on     -- not 'parallel': rows must be applied in commit order
    );
