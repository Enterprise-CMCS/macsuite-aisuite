# AISuite RAG service

Application code for the CMCS Agentic RAG pipeline (Phase 2 packaging).

## Architecture

### Cloud layout

Each environment deploys an internal ALB and ECS Fargate RAG API, on-demand ECS
batch tasks (pre-processing, RAG process, DB bootstrap), and private RDS
Postgres with pgvector. S3, Secrets Manager, ECR, and Bedrock sit outside the
VPC but in the same account/region.

![AISuite Phase 2 architecture](../../docs/aisuite-phase2-architecture.drawio.png)

*Alt text: AWS reference architecture for AISuite Phase 2 VPC, ECS, RDS, S3, and Bedrock.*

Open or edit the diagram in [diagrams.net](https://app.diagrams.net/) by loading
`docs/aisuite-phase2-architecture.drawio.png` (PNG with embedded draw.io XML).
See also the [repo root README](../../README.md#architecture).

### Pipeline data-flow

```mermaid
flowchart LR
  docsS3[Documents_S3] --> preProcess[pre_processing]
  preProcess --> bda[Bedrock_BDA]
  bda --> postS3[PostProcessing_S3]
  postS3 --> ragProcess[rag_process]
  ragProcess --> embed[Bedrock_embeddings]
  embed --> pgvector[RDS_pgvector]
  pgvector --> api[RAG_API_agent]
  api --> llm[Bedrock_LLM]
```

1. **Pre-processing** lists source docs under the active contract’s S3 prefix,
   runs Bedrock Data Automation, and writes parsed text/table/image outputs to
   the post-processing bucket.
2. **RAG process** loads those outputs, chunks text, writes split JSON, embeds
   with Bedrock, and stores vectors in the active contract’s embeddings table.
3. **API** answers natural-language questions via search over that table and a
   Bedrock foundation model.

## Running the service

Work from this directory (`services/rag`) so Python packages resolve. Install
deps from `requirements.txt` first. There is no Docker Compose local stack;
local runs still need AWS access (S3, Bedrock) and a reachable Postgres
(pgvector), or Secrets Manager credentials for the private RDS (often via
VPN/bastion).

### Entry points

| Mode | Local command | Cloud |
|------|---------------|--------|
| Query API | `python -m search.routes.endpoint` (or Docker image `CMD` → uvicorn) | Always-on ECS Fargate service on port `8001` |
| Pre-process batch | `python -m data_preprocessing.pre_processing` | ECS on-demand task |
| Embeddings batch | `python -m data_embeddings_storage.rag_process` | ECS on-demand task |
| DB bootstrap | usually not local (needs master secret) | One-shot Fargate task: `python -m data_embeddings_storage.database.bootstrap` |

Typical ingest order after documents land in the input prefix:

```sh
python -m data_preprocessing.pre_processing
python -m data_embeddings_storage.rag_process
```

### Query API (local)

```sh
export API_HOST=0.0.0.0
export API_PORT=8001
# Optional: API_RELOAD=true
python -m search.routes.endpoint
```

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Health check |
| `GET /agent?query=…` | Agent answer (query string) |
| `POST /agent` | Agent answer; body `{"query": "…"}` |
| `POST /requirements` | Batch requirement grading (synchronous) |
| `GET /contracts` | Available contract IDs and the default |
| `GET/POST /query` | GraphQL |
| `GET /docs` | OpenAPI UI |

### Authentication

The API key is stored in Secrets Manager as `aisuite/<env>/rag-api-key`, using
the JSON key `apiKey`. Authorized callers send that value in the `x-api-key`
header. `/health` is the only unauthenticated route.

Rotation is manual: rotate the secret value in Secrets Manager, then restart or
redeploy the API tasks so the process cache refreshes. There is no rotation
Lambda. CORS origins come from `API_ALLOWED_ORIGINS` as a comma-separated list
and default to empty.

Example:

```sh
curl -s "http://127.0.0.1:8001/agent?query=What+is+coverage+for+NEMT%3F" \
  -H "x-api-key: $API_KEY"
curl -s -X POST "http://127.0.0.1:8001/agent" \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What is coverage for NEMT?"}'
curl -s -X POST "http://127.0.0.1:8001/requirements" \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"requirements":[{"id":"row-12","text":"The Contractor shall provide NEMT services statewide."}],"retry_unclear":true}'
```

`POST /requirements` is synchronous request/response. The batch is capped at
`MAX_BATCH_SIZE` (default 25, override with `REQUIREMENTS_MAX_BATCH_SIZE`) and
each item `text` is capped at 2000 characters. Envelope validation failures
return 4xx without invoking the model; a failing item returns
`Recommendation: "ERROR"` with HTTP 200 for the rest of the batch. This route
grades against the single active `[contract:*]` embeddings table and does **not**
accept a per-request `contract_id` (use `GET`/`POST /agent` for that).

### Verdict persistence

Verdict persistence uses `aisuite_schema.verdicts` for one row per graded
requirement and `aisuite_schema.verdict_chunks` for its ranked supporting
chunks. A verdict row records the requirement text and SHA-256, the
`MET` / `NOT MET` / `UNCLEAR` / `ERROR` result, response, source and page,
foundation and embedding models, prompt version and SHA-256, model settings,
retrieval JSON, latency, and `schema_version`. A chunk row records its verdict
and embeddings-row pointers, rank, document and page, nullable `distance`,
relevance score, `retrieval_leg`, `fusion_rank`, `rerank_score`, and chunk
SHA-256.

`VERDICT_PERSISTENCE_ENABLED` is off by default in code; CDK sets it to `"true"`
for dev only. `VERDICT_STORE_CHUNK_TEXT` defaults to false, so chunks retain a
pointer, scores, and SHA-256 rather than copied text. Do not enable chunk-text
storage in dev without a recorded decision.

DDL is bootstrap-only and never runs on the application/search path. The master
SQL is `scripts/sql/init-aisuite-schema.sql`; the Python bootstrap uses
`ensure_verdict_tables` in
`data_embeddings_storage/database/bootstrap.py`. In v1, the only write site is
`POST /requirements` through `search/requirements/verdicts.py`.
`GET`/`POST /agent` free-text requests are not persisted. Persisted rows always
use `source = requirements_batch`; `client` is currently always `NULL`.

#### Eval read contract

The read contract is `schema_version = 1`. An eval harness may rely on:

- `verdicts`: `id`, `created_at`, `request_id`, `source`, `client`,
  `contract_id`, `embeddings_table`, `requirement_text`,
  `requirement_sha256`, `verdict`, `response_text`, `source_text`, `page_text`,
  `raw_output`, `parsed_ok`, `model_id`, `embed_model_id`, `prompt_version`,
  `prompt_sha256`, `model_settings`, `retrieval`, `latency_ms`, and
  `schema_version`;
- `verdict_chunks`: `verdict_id`, `rank`, `embeddings_table`,
  `embedding_row_id`, `doc_name`, `page`, nullable `distance`,
  `relevance_score`, `retrieval_leg`, `fusion_rank`, `rerank_score`,
  `chunk_sha256`, and nullable `chunk_text`.

#### Privacy and retention open questions

- No PHI/PII determination is recorded in this repo. Contract language is
  assumed to be public procurement text, not PHI; this is an assumption, not a
  determination.
- Requirement text and model explanations are free text, so minimization
  applies.
- Chunk text is not copied unless `VERDICT_STORE_CHUNK_TEXT=true`.
- No retention, TTL, or purge job exists or is planned here. Retention and
  deletion requirements must be answered before qa, uat, or prod enablement.

### Excel CRT client

`search.excel_process.process_excel_with_rag` posts requirement rows to
`POST /requirements`. It never calls the agent in-process. The input workbook is
not mutated; verdicts are written to an output copy.

```sh
python -m search.excel_process.process_excel_with_rag \
  --input /path/to/crt.xlsx \
  --output /path/to/crt_rag_results.xlsx \
  --api-url http://127.0.0.1:8001 \
  --max-rows 25 \
  --batch-size 25
```

`--api-url` defaults to `REQUIREMENTS_API_URL` or `http://127.0.0.1:8001`.
`--input` is required. `--output`, `--max-rows`, and `--batch-size` are optional.

The client requires `AISUITE_EVAL_API_KEY` (or the `API_KEY` alias) and sends it
as the `x-api-key` header. It fails fast if neither is set.

### Evaluation

See the [evaluation guide](eval/README.md) to extract human CRT labels, run an
opt-in live evaluation, and score predictions offline.

### Cloud operation

- **API**: deployed via the AISuite stack (desired count gated by
  `aisuite:activateApi`). Traffic is internal ALB only.
- **Batch / bootstrap**: run the corresponding ECS task definitions on the
  private cluster/subnets. Stack outputs include bootstrap cluster name, task
  definition ARN, security group, and subnet IDs (`RunTask` contract).
- **Automatic ingestion (dev only)**: an S3 `Object Created` event under the
  active contract's `input_prefix` in the documents bucket flows through
  EventBridge to a Step Functions workflow. After a debounce wait, it runs
  `aisuite-dev-pre-processing` and then `aisuite-dev-rag-process`.
- Bucket names and model IDs are injected via ECS environment variables (see
  below), which override matching keys in `aws.properties.ini`.

The debounce window defaults to 5 minutes and is configurable at synth with
`aisuite:ingestionDebounceMinutes`. After the wait, only the oldest `RUNNING`
execution proceeds; other running executions succeed without re-running the
tasks. Step Functions `ListExecutions` is eventually consistent, so a narrow
overlap remains possible. Because pre-processing uses `full_refresh`, overlapping
runs can delete each other's output.

The infrastructure phase (desired count 0) passes
`aisuite:activateIngestion=false`. The activation phase, after images exist,
passes `aisuite:activateIngestion=true` for dev. Omit the context, set it to
`false`, or disable the EventBridge rule to stop automatic runs. The rule is
always `DISABLED` in non-dev environments.

Operators can still use ECS `RunTask` directly, running the
`aisuite-<env>-pre-processing` task definition before
`aisuite-<env>-rag-process`. The stack outputs `IngestionStateMachineArn`,
`IngestionAlertTopicArn`, `IngestionRuleName`, and `IngestionActiveContract`.
The SNS alert topic is created by the stack; subscriptions are added
out-of-band.

The active contract is resolved from `common/utils/aws.properties.ini` at synth
time. The EventBridge rule watches the `input_prefix` that was active then;
`IngestionActiveContract` shows the resolved contract. Re-synthesize and deploy
after changing the active contract.

## Environment variables

Path, model, and table defaults live in
[`common/utils/aws.properties.ini`](common/utils/aws.properties.ini). Comments
at the top of that file list env → property mappings. `Helper` applies env
overrides first, then the active `[contract:…]` section, then `[default]`.

### Cloud (ECS-injected)

| Variable | Role |
|----------|------|
| `AWS_REGION` / `AWS_DEFAULT_REGION` | AWS region |
| `DOCUMENTS_BUCKET` | Source documents bucket |
| `POST_PROCESSING_BUCKET` | BDA / RAG post-processing bucket |
| `PIPELINE_TEMP_BUCKET` | Temp / summary logs bucket |
| `DB_SECRET_ARN` (or `DB_SECRET_NAME`) | Secrets Manager secret for app DB credentials |
| `BEDROCK_MODEL_ID` | Foundation LLM |
| `BEDROCK_EMBED_MODEL_ID` | Embedding model |
| `EMBEDDINGS_TABLE_NAME` | Optional override of active contract table name |

**Bootstrap task only** (from Secrets Manager at task start; never put in task
env plain text, workflow args, or logs):

| Variable | Role |
|----------|------|
| `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, `PGPASSWORD` | Master DB connection |
| `APP_PASSWORD` | Password for `aisuite_app` |

### Local development

| Variable | Role |
|----------|------|
| Standard AWS credential chain | S3, Bedrock, Secrets Manager (no profile names required in docs) |
| `AIPropFile` | Path to an alternate properties INI (absolute or relative) |
| `db_host`, `db_name`, `db_user`, `db_password`, `db_port` | Direct Postgres; if all of host/name/user/password are set, Secrets Manager is skipped |
| `DB_SECRET_ARN` / `DB_SECRET_NAME` | Used when local `db_*` vars are not fully set |
| `API_HOST`, `API_PORT`, `API_RELOAD` | API bind (defaults `0.0.0.0`, `8001`, `false`) |
| Same bucket/model vars as cloud | Optional overrides over the shipped INI |

Optional tuning (INI fallback if unset): `embedding_dimension`,
`embedding_batch_size`, `db_pool_min`, `db_pool_max`.

Constraints: private RDS is not open to the public internet; Bedrock and S3
need valid account permissions for the non-prod (or prod) account you target.

## Specifying documents and contracts

There is **no per-file CLI** and no document-id filter on search.

### Ingest (batch)

Pre-processing lists **all** `.pdf` and `.docx` objects under
`input_bucket_name` + the active contract’s `input_prefix`.

To focus work on a particular file:

1. Upload that object under the contract prefix (or only that object present), **or**
2. Temporarily set `input_prefix` to a narrower key prefix that contains only
   what you want, **or**
3. Switch the active contract (see below) if the document belongs to another
   contract’s tree.

Then re-run pre-process → rag_process. Embeddings write to the active
contract’s `embeddings_table_name`.

### Search (API)

The agent accepts a natural-language `query` and an optional `contract_id` on
both `GET /agent` and `POST /agent`. Contract selection follows this precedence:
request `contract_id` → `EMBEDDINGS_TABLE_NAME` env → active INI section. The
environment and INI defaults apply only when `contract_id` is omitted. Use
`GET /contracts` to list valid contract IDs and identify the default.

The default agent tool is `hybrid_search`; `semantic_search` remains available as
a cheaper fallback. Hybrid search runs Postgres full-text search with
`plainto_tsquery` and vector search, combines the ranked lists with Reciprocal
Rank Fusion (`k = 60`), then reranks the fused candidates with Bedrock Cohere
`cohere.rerank-v3-5:0`. If reranking fails, the query still succeeds and returns
the fused RRF order.

The full-text leg receives the normalized query, while the vector leg receives
the expanded query. Query embeddings use Cohere `input_type=search_query`;
corpus ingest embeddings continue to use `search_document`, so no corpus
re-embedding is required.

`analyze_requirement_with_rag` was removed. `POST /requirements` and the Excel
client share `search.requirements.verdicts`; the Excel script is an HTTP client
of that endpoint. Responses cite document names and pages from `doc_name` and
`page` metadata; the system prompt forbids citing `Hybrid Search Results` as a
source.

Run the RAG unit tests from a project `.venv` (system `python3` is often PEP 668
managed). This command is a per-task acceptance gate and is **not** wired into
GitHub Actions by this change:

```sh
cd services/rag && python3 -m unittest discover -s tests -p 'test_*.py'
```

## Per-contract configuration (dev)

Multi-contract isolation uses shared S3 buckets with per-contract prefixes and a
per-contract embeddings table on the same RDS instance.

| Role | Dev bucket |
|------|------------|
| Source documents (input) | `aisuite-dev-contract-rag` |
| BDA / RAG post-processing | `aisuite-dev-contract-rag-post-processing` |
| Pipeline code | `aisuite-dev-llm-pipeline-code` |
| Pipeline temp / summary logs | `aisuite-dev-llm-pipeline-temp` |

Contract-specific paths and `embeddings_table_name` live in `[contract:…]` sections of
`common/utils/aws.properties.ini`. ECS still overrides bucket names via env
(`DOCUMENTS_BUCKET`, `POST_PROCESSING_BUCKET`, `PIPELINE_TEMP_BUCKET`).

### Active contract

Exactly one section may have `active = true`. This flag sets only the default
for requests that omit `contract_id`; the dev default is `tn_6756` (TennCare).
Another contract can be selected per request without switching the flag or
redeploying. To change the default:

1. Set `active = true` on the target `[contract:…]` section.
2. Set `active = false` on the previously active section.
3. Leave prefixes and `embeddings_table_name` alone unless rewiring storage.
4. Restart / redeploy tasks so they reload the INI.

A valid contract can still have an empty embeddings table if it was never
ingested; queries against it can therefore return `UNCLEAR`.

### Contract matrix (dev)

| ID | Active | `input_prefix` | `embeddings_table_name` |
|----|--------|----------------|-------------------------|
| `me_0002` | false | `state_of_ME/MCR-ME-0002-NEMT/` | `embeddings_me_0002_nemt` |
| `tn_6756` | true | `state_of_TN/MCCRS-TN-6756-TennCare/` | `embeddings_tn_6756_tenncare` |
| `wa_6369` | false | `state_of_WA/MCCRS-WA-6369-IFC/` | `embeddings_wa_6369_ifc` |
| `wa_6472` | false | `state_of_WA/MCCRS-WA-6472-AHIMC/` | `embeddings_wa_6472_ahimc` |
| `wa_6473` | false | `state_of_WA/MCCRS-WA-6473-IFC/` | `embeddings_wa_6473_ifc` |

Full `output_prefix` / BDA / RAG folder paths are in the INI under each contract section.

### Bootstrap note

Bootstrap creates each contract’s embeddings table (and indexes) so switching `active`
does not require a re-run for DDL. Ingestion uses the active contract table;
search can select a configured contract per request.

## Reindex after a chunking change

This change does not silently mutate existing embeddings or run a reindex.
Operators must reindex each contract that should use the new chunks; other
contracts' tables continue to contain the previous generation until they are
reindexed.

Embeddings writes are append-only. The RAG process uses
`INSERT INTO {table} (text, metadata, embedding)` in
`data_embeddings_storage/database/data_processing_embeddings.py`; there is no
upsert, delete, or chunk-identity unique key. Re-running the `rag-process` task
without resetting its table therefore duplicates rows instead of replacing
them.

### Preferred in dev: truncate and reload

**Warning: this is destructive and dev-only.** For the default active contract,
remove every existing TennCare embedding row before loading the replacement
generation:

```sql
TRUNCATE TABLE aisuite_schema.embeddings_tn_6756_tenncare;
```

After the truncate completes, re-run `rag-process`. Re-run pre-processing first
only when the split JSON in post-processing storage is also stale. Do not use
this procedure outside dev without an environment-specific recovery and
cutover plan.

### Blue/green alternative

For a non-destructive cutover:

1. Choose a new `embeddings_table_name`.
2. Set that name on the active `[contract:*]` section in
   `common/utils/aws.properties.ini`.
3. Run bootstrap so the new table and indexes exist.
4. Run `rag-process` and verify the new table's row count.
5. Flip search to the new table only after verification.

Event-driven ingestion pins its prefix and table at synth time. An INI table
rename therefore requires bootstrap and a re-synth. The activation phase of the
dev deployment workflow (not the desiredCount-0 infrastructure phase) passes
`aisuite:activateIngestion=true`. Do not arm the trigger mid-reindex if
the first chunking reindex is still in progress; keep it disabled until that
reindex finishes so a synth cannot retarget the trigger during the operation.

### Runtime, cost, and generation visibility

Every chunk causes a Bedrock embedding call. The RAG process invokes
`EmbeddingProcessor.process_data(..., batch_size=30)` in `creating_main.py`,
while `data_processing_embeddings.py` defaults
`max_concurrent_requests` to `1`. Changing chunking changes the chunk count and
therefore the embedding call volume, runtime, and cost.

TEXT chunks stamp `chunking_version` (currently `v2-recursive-cascade` from
`chunk_documents.py`), `chunk_index`, and `chunk_count` into the metadata JSONB
column. Mixed-generation tables can be observed with
`metadata->>'chunking_version'`, but they are not automatically reconciled.

## One-time DB bootstrap (app user)

CDK manages an on-demand Fargate task that bootstraps the private RDS database.
It reuses the batch ECS cluster, ECR repository, immutable batch image tag,
private subnets, and application security group. It is a one-shot task
definition, not an ECS service.

Run the task after the initial infrastructure deploy and after pushing the
corresponding batch image. The stack outputs `BootstrapClusterName`,
`BootstrapTaskDefinitionArn`, `BootstrapSecurityGroupId`, and
`BootstrapSubnetIds` as the `RunTask` contract. Deployment workflow automation
is handled separately.

ECS injects `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, and `PGPASSWORD` from
the master database secret and `APP_PASSWORD` from the app database secret.
The values are resolved by the ECS execution role at task startup; they must
not be supplied through task-definition environment values, workflow
arguments, or logs.

The bootstrap entrypoint is
`data_embeddings_storage.database.bootstrap`. It runs idempotently and:

- creates the `vector` and `pg_trgm` extensions;
- creates the dedicated `aisuite_schema` (not `public`) for application objects;
- migrates any legacy `public.embeddings*` tables into `aisuite_schema`;
- creates each contract’s embeddings table (from `aws.properties.ini`) and HNSW,
  metadata GIN, trigram GIN, and full-text GIN indexes matching `table_setup.py`;
- creates `verdicts`, `verdict_chunks`, and their indexes in `aisuite_schema`;
- creates `aisuite_app` or safely updates its password from the app secret;
- creates the shared `aisuite_app_owner` NOLOGIN group role, grants it to
  both rotation logins, and reassigns `aisuite_schema` (tables and sequences)
  to that owner so index DDL succeeds under either Secrets Manager login;
- grants `CONNECT` on the database and schema/table privileges on
  `aisuite_schema` to `aisuite_app` and `aisuite_app_clone` (Secrets Manager
  multi-user rotation), including `CREATE` on the app schema for idempotent
  table setup;
- sets each app role’s `search_path` to `aisuite_schema, public`; and
- grants default table DML and sequence usage/select privileges in
  `aisuite_schema` for objects created by the bootstrap master role.

Runtime connections set `search_path` on pool init so unqualified table names
resolve to `aisuite_schema` regardless of which rotation login is active.
A fresh environment needs bootstrap (or the master SQL remediation below)
before the app path can create indexes.

### Operator remediation (VPN + CLI)

```sh
export AWS_PROFILE=aisuite-dev
./scripts/psql-rag.sh --master -v ON_ERROR_STOP=1 -f scripts/sql/init-aisuite-schema.sql
./scripts/psql-rag.sh --probe
```

`init-aisuite-schema.sql` creates the schema, migrates leftover
`public.embeddings*`, creates the verdict tables and indexes, installs
`aisuite_app_owner`, reassigns object ownership, and grants both app roles.
Embeddings tables and indexes remain driven by `aws.properties.ini` and created
through the Python bootstrap path.

After bootstrap, applications use
`aisuite/{env}/rag-app-db-credentials`, not the master
`rag-db-credentials` secret. Schema DDL remains a privileged bootstrap
responsibility.
