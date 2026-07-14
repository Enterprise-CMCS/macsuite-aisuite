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
