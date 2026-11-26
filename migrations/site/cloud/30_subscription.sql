-- site/cloud/30_subscription.sql: cloud database only. Subscribes to host A's
-- pub_a. Run this before site/a/30_subscription.sql; see that file for the
-- full order.
--
-- copy_data = false: run this while host A has written nothing yet, so there
-- is nothing to copy, and the slot created on host A captures every later
-- change. Copying would also send back the cloud rows host A received during
-- its own initial sync and collide on primary keys.
--
-- The connection string goes in a psql variable; keep the password in the
-- postgres user's ~/.pgpass on the cloud server, not here:
--   psql -d invisiscan -v a_conninfo='host=10.8.0.2 port=5432 dbname=invisiscan user=invisiscan_repl' \
--        -f 30_subscription.sql

CREATE SUBSCRIPTION sub_a
    CONNECTION :'a_conninfo'
    PUBLICATION pub_a
    WITH (
        origin    = none,  -- do not receive changes host A applied from the cloud
        copy_data = false,
        streaming = on     -- not 'parallel': domain_events cursors need commit order
    );
