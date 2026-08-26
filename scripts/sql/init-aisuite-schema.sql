-- Idempotent schema/grants for aisuite_schema (master credentials).
-- ./scripts/psql-rag.sh --master -f scripts/sql/init-aisuite-schema.sql

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS aisuite_schema;

-- Move any legacy public.embeddings* tables created before aisuite_schema existed.
DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT tablename
    FROM pg_tables
    WHERE schemaname = 'public'
      AND tablename LIKE 'embeddings%'
    ORDER BY tablename
  LOOP
    EXECUTE format(
      'ALTER TABLE public.%I SET SCHEMA aisuite_schema',
      r.tablename
    );
  END LOOP;
END $$;

-- Shared NOLOGIN owner role so both Secrets Manager rotation logins can
-- satisfy Postgres ownership checks (CREATE INDEX, ALTER TABLE, etc.).
DO $$
DECLARE
  r RECORD;
  role_name text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aisuite_app_owner') THEN
    CREATE ROLE aisuite_app_owner NOLOGIN;
  END IF;
  EXECUTE format('GRANT aisuite_app_owner TO %I', current_user);

  FOREACH role_name IN ARRAY ARRAY['aisuite_app', 'aisuite_app_clone']
  LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
      EXECUTE format('GRANT %I TO %I', role_name, current_user);
    END IF;
  END LOOP;

  EXECUTE 'ALTER SCHEMA aisuite_schema OWNER TO aisuite_app_owner';

  FOR r IN
    SELECT tablename
    FROM pg_tables
    WHERE schemaname = 'aisuite_schema'
    ORDER BY tablename
  LOOP
    EXECUTE format(
      'ALTER TABLE aisuite_schema.%I OWNER TO aisuite_app_owner',
      r.tablename
    );
  END LOOP;

  FOR r IN
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'aisuite_schema' AND c.relkind = 'S'
    ORDER BY c.relname
  LOOP
    EXECUTE format(
      'ALTER SEQUENCE aisuite_schema.%I OWNER TO aisuite_app_owner',
      r.relname
    );
  END LOOP;
END $$;

-- App role is expected to exist (created by Secrets Manager / bootstrap).
-- Multi-user rotation may also create aisuite_app_clone; grant both when present.
DO $$
DECLARE
  role_name text;
  db_name text := current_database();
BEGIN
  FOREACH role_name IN ARRAY ARRAY['aisuite_app', 'aisuite_app_clone']
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
      CONTINUE;
    END IF;

    EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', db_name, role_name);
    EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', role_name);
    EXECUTE format(
      'GRANT USAGE, CREATE ON SCHEMA aisuite_schema TO %I',
      role_name
    );
    EXECUTE format(
      'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES '
      'IN SCHEMA aisuite_schema TO %I',
      role_name
    );
    EXECUTE format(
      'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA aisuite_schema TO %I',
      role_name
    );
    EXECUTE format(
      'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA aisuite_schema TO %I',
      role_name
    );
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES IN SCHEMA aisuite_schema '
      'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
      role_name
    );
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES IN SCHEMA aisuite_schema '
      'GRANT USAGE, SELECT ON SEQUENCES TO %I',
      role_name
    );
    EXECUTE format('GRANT aisuite_app_owner TO %I', role_name);
    EXECUTE format('ALTER ROLE %I INHERIT', role_name);
    EXECUTE format(
      'ALTER ROLE %I SET search_path TO aisuite_schema, public',
      role_name
    );
  END LOOP;
END $$;
