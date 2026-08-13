# Post-backlog look-forward

Presenter and team runbook for a CRT-slice showcase after the nine explainer follow-ons land on `bp-forward`.
The stakeholder talk track is the HTML sequel next to the August explainer.
This page is how we prove the path, size the API, and abort if the live batch dies.

True now means: those nine child specs are `done`, in `dev`.

## Shared true / not / next

Same table as the HTML. Do not add rows.

### True now

- Batch grade of requirements (explainer gap 1 / idea 2)
- API key on non-health routes (gap 2 / idea 7 partial)
- Per-request contract scoping (gap 4 / idea 3)
- Hybrid search, rerank, and query-embedding fix (gap 5 / idea 1 / defect 1)
- Offline eval harness exists; live eval is opt-in (gap 7 / idea 5) — numbers need a labeled CRT workbook
- Verdict persistence available in `dev` (idea 8)
- Recursive chunking (defect 5); pandas pin (defect 2)
- Event-driven ingestion exists; dev deploy currently arms it
- ALB idle timeout 300s; API still internal-only HTTP unless a cert ARN is filled

### Not true yet

- TLS in transit (cert ARN map is empty)
- qa / uat / prod actually running the app (explainer idea 4 / gap 6; program out of scope)
- Salesforce CRT filling reviewer fields (OY2-40312)
- ATO and Datadog epics
- Measured human agreement (workbook is a human input, not in git)
- A browser UI, so no Playwright e2e
- Python tests in CI (locked unless explicitly approved)
- Explainer ideas 9–11 (compare, amendment diff, confidence routing)
- PHI/PII determination for verdicts outside `dev`

### Next, in this order

1. Pre-showcase prove: HTTP smoke + a CRT-slice rehearsal on the same path the presenter will use
2. Capacity bump before the room
3. Showcase: presenter on VPN/CMS network projects a live CRT slice
4. Labeled CRT workbook if the room should hear agreement numbers
5. ACM cert + DNS so TLS can actually turn on
6. PHI determination before enabling verdict store outside `dev`
7. Then, separately: qa activation, Salesforce, ATO/Datadog, ideas 9–11

## How we know the nine units work

Each child already ran the AGENTS.md gates: type-check, Vitest, and four-environment synth.
That does not prove live grading.

Prove the room path before the room: real API key, indexed contract, CRT slice, same internal URL the presenter will project.
Optional local check, not CI: Python tests under `services/rag/tests/` (`cd services/rag && python -m unittest discover -s tests`).
Python tests stay out of GitHub Actions unless someone explicitly approves a new gate.

Quality is the eval harness in `services/rag/eval/`, not a browser suite.
Live eval is opt-in via `AISUITE_EVAL_LIVE=1` (see `services/rag/eval/README.md`).
Do not quote an agreement rate until a labeled CRT workbook exists.

## HTTP smoke (not Playwright)

There is no UI.
Do not add Playwright.
"E2E" here means HTTP against the live internal API, then the CRT slice on the same path.

Do not add a CI job for this smoke unless it is later approved.
Python CI is a program-wide lock.

Work from a laptop on VPN or the CMS network.
The ALB is internal-only HTTP on port 80.
Stack output `AlbDnsName` on `aisuite-dev-infrastructure` is the host.
The API key is Secrets Manager `aisuite/dev/rag-api-key`, JSON field `apiKey`.
Send it as `x-api-key`.
`GET /health` is the only unauthenticated route.

```sh
ALB_DNS="$(aws cloudformation describe-stacks \
  --stack-name aisuite-dev-infrastructure \
  --query "Stacks[0].Outputs[?OutputKey=='AlbDnsName'].OutputValue" \
  --output text)"

API_KEY="$(aws secretsmanager get-secret-value \
  --secret-id aisuite/dev/rag-api-key \
  --query SecretString \
  --output text | python -c 'import json,sys; print(json.load(sys.stdin)["apiKey"])')"

# 1. Health — no key
curl -sS "http://${ALB_DNS}/health"

# 2. Contracts — key required
curl -sS "http://${ALB_DNS}/contracts" \
  -H "x-api-key: ${API_KEY}"

# 3. A few rows — key required; idle timeout is 300s
curl -sS -X POST "http://${ALB_DNS}/requirements" \
  -H "x-api-key: ${API_KEY}" \
  -H "Content-Type: application/json" \
  --max-time 300 \
  -d '{"contract_id":"tn_6756","requirements":[{"id":"smoke-1","text":"The Contractor shall provide NEMT services statewide."},{"id":"smoke-2","text":"The Contractor shall maintain a 24-hour member call line."}],"retry_unclear":true}'
```

Confirm `GET /contracts` lists the contract you will grade (`tn_6756` is the shipped default).
Confirm the few-row `POST /requirements` returns `MET` / `NOT MET` / `UNCLEAR` (or `ERROR`) with `Source` and `Page`, not a free-text chat answer.

Then rehearse the CRT slice with the Excel client, from `services/rag`, against that same ALB:

```sh
export AISUITE_EVAL_API_KEY="${API_KEY}"
python -m search.excel_process.process_excel_with_rag \
  --input /path/to/crt-slice.xlsx \
  --output /path/to/crt-slice_rag_results.xlsx \
  --api-url "http://${ALB_DNS}" \
  --max-rows 40 \
  --batch-size 25
```

`--max-rows 40` is a dozens-row slice, not a full CRT dump.
Default batch cap is 25 (`REQUIREMENTS_MAX_BATCH_SIZE`).
Use `--max-time 300` / the ALB idle timeout of 300s as the ceiling; if a batch hangs past that, it is dead.

## Capacity bump before the room

Do not implement the bump in this write-up.
Treat it as a pre-showcase condition.

The `dev` API is 256 CPU / 512 MiB (`src/constructs/compute-construct.ts`).
A dozens-row CRT slice with hybrid search and rerank is why that size is too small for the room.
Recommend at least the existing uat/prod size as the floor: **512 CPU / 1024 MiB**.
Batch tasks are already 1 vCPU / 2 GiB (`src/constructs/batch-construct.ts`); the constraint is the always-on API, not ingest.

TLS still needs a program-controlled DNS name and ACM cert ARN.
`DEPLOYMENT_ENVIRONMENT_API_CERTIFICATE_ARN` in `src/deployment-config.ts` is empty.
Filling it is not this runbook.

## Presenter checklist

- VPN or CMS network; the ALB is not on the public internet.
- API key from Secrets Manager `aisuite/dev/rag-api-key` (`apiKey`), not a laptop `.env` committed anywhere.
- Which contract is indexed (default `tn_6756` unless the slice is another state).
- CRT slice file on disk; unlabeled is fine for the mechanism demo; labeled is required before anyone says an agreement percentage.
- Idle-timeout awareness: 300s per request; batch size 25; do not start a full-workbook grade in the room.
- Abort plan: if the first batch errors, hangs past 300s, or returns `ERROR` on most rows, stop. Do not retry a larger slice. Fall back to the already-rehearsed few-row smoke and walk the true / not / next table.
- Attendees never receive the URL or the key.

## After the showcase

Use eval agreement to tune retrieval once a labeled workbook exists, not gut feel.
Python CI only if explicitly approved.
Autoscaling and alarms belong with the Datadog epic, not this page.

## Future (directional)

Named, not specified, and not this write-up's build list:

- Explainer ideas 9–11 (contract-to-contract compare, amendment diff, confidence routing)
- Salesforce CRT filling reviewer fields (OY2-40312)
- ATO and Datadog
- qa / uat / prod runtime activation
