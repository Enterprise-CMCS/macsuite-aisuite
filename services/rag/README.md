# AISuite RAG service

Application code for the CMCS Agentic RAG pipeline (Phase 2 packaging).

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
(`DOCUMENTS_BUCKET`, `POST_PROCESSING_BUCKET`, `PIPELINE_CODE_BUCKET`,
`PIPELINE_TEMP_BUCKET`).

### Active contract

Exactly one section may have `active = true`. Default for dev is `tn_6756` (TennCare).
To switch:

1. Set `active = true` on the target `[contract:…]` section.
2. Set `active = false` on the previously active section.
3. Leave prefixes and `embeddings_table_name` alone unless rewiring storage.
4. Restart / redeploy tasks so they reload the INI.

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
does not require a re-run for DDL. Search/store use only the active contract table.

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
- creates each contract’s embeddings table (from `aws.properties.ini`) and HNSW,
  metadata GIN, trigram GIN, and full-text GIN indexes matching `table_setup.py`;
- creates `aisuite_app` or safely updates its password from the app secret;
- grants `CONNECT` on the database, `USAGE` on the `public` schema, table
  `SELECT`/`INSERT`/`UPDATE`/`DELETE`, sequence `USAGE`/`SELECT`, and function
  `EXECUTE`; and
- grants default table DML and sequence usage/select privileges in `public`.

After bootstrap, applications use
`aisuite/{env}/rag-app-db-credentials`, not the master
`rag-db-credentials` secret. Schema DDL remains a privileged bootstrap
responsibility.
