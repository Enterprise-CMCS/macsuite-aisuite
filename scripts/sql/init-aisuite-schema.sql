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

-- Verdict persistence tables (mirrors
-- services/rag/data_embeddings_storage/database/verdict_schema.py).
CREATE TABLE IF NOT EXISTS aisuite_schema.verdicts (
  id BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  request_id UUID NOT NULL,
  source TEXT NOT NULL,
  client TEXT,
  contract_id TEXT NOT NULL,
  embeddings_table TEXT NOT NULL,
  requirement_text TEXT NOT NULL,
  requirement_sha256 TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (
    verdict IN ('MET', 'NOT MET', 'UNCLEAR', 'ERROR')
  ),
  response_text TEXT,
  source_text TEXT,
  page_text TEXT,
  raw_output TEXT,
  parsed_ok BOOLEAN NOT NULL,
  model_id TEXT NOT NULL,
  embed_model_id TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  model_settings JSONB,
  retrieval JSONB,
  latency_ms INTEGER,
  schema_version SMALLINT NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS aisuite_schema.verdict_chunks (
  id BIGSERIAL PRIMARY KEY,
  verdict_id BIGINT NOT NULL REFERENCES aisuite_schema.verdicts(id)
    ON DELETE CASCADE,
  rank SMALLINT NOT NULL,
  embeddings_table TEXT NOT NULL,
  embedding_row_id INTEGER,
  doc_name TEXT,
  page TEXT,
  distance DOUBLE PRECISION,
  relevance_score DOUBLE PRECISION,
  retrieval_leg TEXT,
  fusion_rank SMALLINT,
  rerank_score DOUBLE PRECISION,
  chunk_sha256 TEXT NOT NULL,
  chunk_text TEXT,
  UNIQUE (verdict_id, rank)
);

CREATE INDEX IF NOT EXISTS idx_verdicts_created_at
  ON aisuite_schema.verdicts (created_at);
CREATE INDEX IF NOT EXISTS idx_verdicts_requirement_sha256
  ON aisuite_schema.verdicts (requirement_sha256);
CREATE INDEX IF NOT EXISTS idx_verdicts_contract_id_created_at
  ON aisuite_schema.verdicts (contract_id, created_at);
CREATE INDEX IF NOT EXISTS idx_verdict_chunks_verdict_id
  ON aisuite_schema.verdict_chunks (verdict_id);

-- Shared NOLOGIN owner role so both Secrets Manager rotation logins can
-- satisfy Postgres ownership checks (CREATE INDEX, ALTER TABLE, etc.).
DO $$
DECLARE
  r RECORD;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aisuite_app_owner') THEN
    CREATE ROLE aisuite_app_owner NOLOGIN;
  END IF;
  EXECUTE format('GRANT aisuite_app_owner TO %I', current_user);

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
