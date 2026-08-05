#!/usr/bin/env bash
# Copy legacy cmcs-contract-* buckets into aisuite-dev-* counterparts across
# accounts (no cross-account IAM). Flow: download with source profile, then
# upload with dest profile, restoring each object's Content-Type from the source.
#
# Edit the profiles below, then run: ./scripts/sync-dev-s3-buckets.sh

set -euo pipefail

# --- edit me ---
SOURCE_AWS_PROFILE="YOUR_SOURCE_PROFILE"
DEST_AWS_PROFILE="YOUR_DEST_PROFILE"
AWS_REGION="us-east-1"
# ---------------

STAGE_ROOT="${STAGE_ROOT:-${TMPDIR:-/tmp}/aisuite-dev-s3-sync}"

cleanup_stage() {
  rm -rf "${STAGE_ROOT}"
}

trap cleanup_stage EXIT

head_field() {
  local bucket="$1"
  local key="$2"
  local field="$3"
  local value
  value="$(
    aws --profile "${SOURCE_AWS_PROFILE}" --region "${AWS_REGION}" \
      s3api head-object \
      --bucket "${bucket}" \
      --key "${key}" \
      --query "${field}" \
      --output text 2>/dev/null || true
  )"
  if [[ -z "${value}" || "${value}" == "None" ]]; then
    return 1
  fi
  printf '%s' "${value}"
}

copy_pair() {
  local src="$1"
  local dst="$2"
  local stage="${STAGE_ROOT}/${src}"
  local file key content_type extra_args count

  echo "=== s3://${src}/ -> s3://${dst}/ ==="
  rm -rf "${stage}"
  mkdir -p "${stage}"

  echo "Downloading (profile=${SOURCE_AWS_PROFILE})"
  aws --profile "${SOURCE_AWS_PROFILE}" --region "${AWS_REGION}" \
    s3 sync "s3://${src}/" "${stage}/"

  echo "Uploading (profile=${DEST_AWS_PROFILE})"
  count=0
  while IFS= read -r -d '' file; do
    key="${file#"${stage}"/}"
    extra_args=()

    if content_type="$(head_field "${src}" "${key}" "ContentType")"; then
      extra_args+=(--content-type "${content_type}")
    fi

    aws --profile "${DEST_AWS_PROFILE}" --region "${AWS_REGION}" \
      s3 cp "${file}" "s3://${dst}/${key}" \
      --sse AES256 \
      "${extra_args[@]+"${extra_args[@]}"}" \
      >/dev/null

    count=$((count + 1))
    printf '\rUploaded %d object(s)' "${count}"
  done < <(find "${stage}" -type f -print0)

  echo
  rm -rf "${stage}"
}

copy_pair "cmcs-contract-rag" "aisuite-dev-contract-rag"
copy_pair "cmcs-contract-rag-post-processing" "aisuite-dev-contract-rag-post-processing"
copy_pair "cmcs-contract-llm-pipeline-code" "aisuite-dev-llm-pipeline-code"
copy_pair "cmcs-contract-llm-pipeline-temp" "aisuite-dev-llm-pipeline-temp"

echo "Done."
