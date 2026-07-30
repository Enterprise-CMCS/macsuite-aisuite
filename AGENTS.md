# AGENTS.md

Greenfield TypeScript AWS CDK infra for AISuite (OY2-40308). Entry: `bin/app.ts`. Stacks and config: `src/`.

## Tooling

- Node: CI/local pin in `.nvmrc` (`nvm use`); `engines.node` allows `>=22`
- Package manager: `pnpm@11.7.0` via `packageManager` — enable with `corepack enable`, or use `npx --yes pnpm@11.7.0` if needed
- Tests: Vitest · Infra: AWS CDK

## Layout

- `src/` — CDK stacks and deployment config
- `test/` — Vitest unit tests
- `bin/` — CDK app entry
- `.github/workflows/` — CI and deploy
- `.github/oidc/` — GitHub OIDC deploy roles

## Environments

- `dev` / `qa` / `uat` → shared non-prod AWS account
- `prod` → prod account
- OIDC wiring: `.github/oidc/README.md`

## Validate before done

```sh
nvm use
corepack enable
pnpm install --frozen-lockfile
pnpm run type-check
pnpm test
pnpm run cdk:synth --context environment=dev
pnpm run cdk:synth --context environment=qa
pnpm run cdk:synth --context environment=uat
pnpm run cdk:synth --context environment=prod
```

Use `pnpm run test:coverage` when coverage is relevant. CI runs the same gates.

## Working rules

- Keep diffs focused on the request.
- Never commit secrets, tokens, or `.env` files.
- Do not invent product/application runtime until it is chosen in-repo.
- Prefer real commands over speculative docs.

## Output budget

- Keep replies short. No task restatement or long recaps.
- Do not create task, decision, or run-summary docs unless asked.
- Do not expand `README.md` or this file unless the change requires it.
- Skip speculative backlog or parking-lot docs.
- Validate with commands; do not write evidence files into the repo.

## Learned User Preferences

- Prefer CDK `Vpc.fromLookup` by CloudTamer VPC name over hardcoding VPC/subnet IDs in source.
- Do not commit CDK VPC lookup context/cache values; CI/deploy synth should produce them.
- In repo docs, avoid AWS profile names and account numbers; prefer outcome wording (e.g. non-prod done, prod pending).
- Base work on `main` with ephemeral feature branches; push only when explicitly asked.
- On large harness runs, often stop before commit until deslop/review/gates finish.

## Learned Workspace Facts

- Agent harness overlay (specs/tasks) lives under `.cursor/harness`.
- CloudTamer VPC Name tags map in config: `dev`→`aisuite-east-dev`, `qa`→`aisuite-east-qa`, `uat`→`aisuite-east-test`, `prod`→`aisuite-east-prod` (keep names; do not rename VPCs).
- Database target is internal-only RDS Postgres 16.
- Near-term delivery focus is `dev`; training PDFs/docs may be missing until uploaded to buckets.
- Jira project CLDSPT exists, but there is no single board or sprint cadence yet.
