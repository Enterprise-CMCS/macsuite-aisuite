# AISuite RAG service

Application code for the CMCS Agentic RAG pipeline (Phase 2 packaging).

## One-time DB bootstrap (app user)

RDS is provisioned with master user `aisuite_admin`. Multi-user Secrets Manager
rotation expects PostgreSQL role `aisuite_app` to already exist. Run once as the
master user after the first deploy (do **not** commit passwords).

Extensions and schema must be created by the master (or another privileged role)
before the app connects as `aisuite_app`. The batch job calls
`CREATE EXTENSION` / `CREATE TABLE` on every run; those statements require
privileges `aisuite_app` does not have.

```sql
-- Replace <password from aisuite/{env}/rag-app-db-credentials> before running.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Create the embeddings table + indexes once (match table_setup.py), then:
CREATE ROLE aisuite_app LOGIN PASSWORD '<app-secret-password>';

GRANT CONNECT ON DATABASE vectordb TO aisuite_app;
GRANT USAGE ON SCHEMA public TO aisuite_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aisuite_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aisuite_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO aisuite_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aisuite_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO aisuite_app;
```

After bootstrap, apps must use secret `aisuite/{env}/rag-app-db-credentials`
(not the master `rag-db-credentials` secret). Prefer making
`init_database` / `table_setup` no-ops when objects already exist under a
least-privilege app user (follow-up), or run schema DDL only via master.
