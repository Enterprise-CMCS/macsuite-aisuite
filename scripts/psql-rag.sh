#!/usr/bin/env bash
# Open an interactive psql session to the AISuite RAG Postgres (pgvector) DB.
#
# Prerequisites:
#   - Network path to the private RDS (VPN / bastion / in-VPC)
#   - aws CLI + jq + psql
#   - AWS credentials that can read the Secrets Manager secret
#
# Usage:
#   ./scripts/psql-rag.sh                  # app user, env=dev
#   ./scripts/psql-rag.sh --env qa
#   ./scripts/psql-rag.sh --master         # master/admin secret (DDL)
#   ./scripts/psql-rag.sh --probe          # SELECT 1 + list embeddings tables
#   ./scripts/psql-rag.sh -c 'SELECT version();'
#
# Env overrides: ENVIRONMENT, AWS_REGION, DB_SECRET_ID, SSLMODE, SSLROOTCERT

set -euo pipefail

ENVIRONMENT="${ENVIRONMENT:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
USE_MASTER=0
PROBE=0
PSQL_ARGS=()

usage() {
  cat <<'EOF'
Usage: psql-rag.sh [--env ENV] [--master] [--probe] [--] [psql args...]

  --env ENV     Deployment environment (default: dev). Sets secret
                aisuite/<ENV>/rag-app-db-credentials
  --master      Use aisuite/<ENV>/rag-db-credentials instead of app secret
  --probe       Run a connection check (SELECT 1 + list embeddings* tables)
                instead of an interactive session
  -h, --help    Show this help

Extra args after -- (or unknown flags that look like psql options) are
passed through to psql. Examples:

  ./scripts/psql-rag.sh --probe
  ./scripts/psql-rag.sh --master -f scripts/sql/init-aisuite-schema.sql
  ./scripts/psql-rag.sh -c '\dt aisuite_schema.*'
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      ENVIRONMENT="${2:?--env requires a value}"
      shift 2
      ;;
    --master)
      USE_MASTER=1
      shift
      ;;
    --probe)
      PROBE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      PSQL_ARGS+=("$@")
      break
      ;;
    *)
      PSQL_ARGS+=("$1")
      shift
      ;;
  esac
done

for cmd in aws jq psql; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "error: required command not found: ${cmd}" >&2
    exit 1
  fi
done

if [[ -n "${DB_SECRET_ID:-}" ]]; then
  SECRET_ID="${DB_SECRET_ID}"
elif [[ "${USE_MASTER}" -eq 1 ]]; then
  SECRET_ID="aisuite/${ENVIRONMENT}/rag-db-credentials"
else
  SECRET_ID="aisuite/${ENVIRONMENT}/rag-app-db-credentials"
fi

echo "Fetching credentials from ${SECRET_ID} (region=${AWS_REGION})" >&2

SECRET_JSON="$(
  aws secretsmanager get-secret-value \
    --region "${AWS_REGION}" \
    --secret-id "${SECRET_ID}" \
    --query SecretString \
    --output text
)"

export PGHOST
export PGPORT
export PGDATABASE
export PGUSER
export PGPASSWORD
PGHOST="$(jq -er '.host // empty' <<<"${SECRET_JSON}")"
PGPORT="$(jq -er '.port // 5432' <<<"${SECRET_JSON}")"
PGDATABASE="$(jq -er '.dbname // empty' <<<"${SECRET_JSON}")"
PGUSER="$(jq -er '.username // empty' <<<"${SECRET_JSON}")"
PGPASSWORD="$(jq -er '.password // empty' <<<"${SECRET_JSON}")"

if [[ -z "${PGHOST}" || -z "${PGDATABASE}" || -z "${PGUSER}" || -z "${PGPASSWORD}" ]]; then
  echo "error: secret ${SECRET_ID} missing host/dbname/username/password" >&2
  exit 1
fi

# Force TLS (PGSSLMODE / conninfo). SSLMODE and SSLROOTCERT override if set.
export PGSSLMODE="${SSLMODE:-require}"
if [[ -n "${SSLROOTCERT:-${PGSSLROOTCERT:-}}" ]]; then
  export PGSSLROOTCERT="${SSLROOTCERT:-${PGSSLROOTCERT}}"
fi

CONNINFO="host=${PGHOST} port=${PGPORT} dbname=${PGDATABASE} user=${PGUSER} sslmode=${PGSSLMODE}"
if [[ -n "${PGSSLROOTCERT:-}" ]]; then
  CONNINFO+=" sslrootcert=${PGSSLROOTCERT}"
fi

echo "Connecting to ${PGUSER}@${PGHOST}:${PGPORT}/${PGDATABASE} (sslmode=${PGSSLMODE})" >&2

PSQL_BASE=(psql "${CONNINFO}")

if [[ "${PROBE}" -eq 1 ]]; then
  if [[ ${#PSQL_ARGS[@]} -gt 0 ]]; then
    echo "error: --probe does not accept extra psql args" >&2
    exit 1
  fi
  "${PSQL_BASE[@]}" -v ON_ERROR_STOP=1 <<'SQL'
SELECT current_database() AS database,
       current_user AS db_user,
       current_schemas(true) AS search_path,
       inet_server_addr() AS server_addr,
       version();
\echo
\dn aisuite_schema
\dt aisuite_schema.*
SQL
  echo "Probe succeeded." >&2
  exit 0
fi

if [[ ${#PSQL_ARGS[@]} -eq 0 ]]; then
  exec "${PSQL_BASE[@]}"
else
  exec "${PSQL_BASE[@]}" "${PSQL_ARGS[@]}"
fi
