# macsuite-aisuite

Greenfield TypeScript AWS CDK application for OY2-40308.

Logical stages `dev` / `qa` / `uat` map to the shared non-prod AWS account;
`prod` maps to the prod account. GitHub Actions assumes an OIDC deploy role per
account — see [`.github/oidc/README.md`](.github/oidc/README.md).

## Setup

```sh
nvm use                 # Node pin in .nvmrc (engines allow Node >=22)
corepack enable         # activates pnpm from packageManager
pnpm install
```

If Corepack is unavailable: `npx --yes pnpm@11.7.0 install`.

## CI and deploy

- **Unit tests** (`.github/workflows/unit-tests.yml`): Vitest on pull requests
  and pushes to `main`.
- **CI** (`.github/workflows/ci.yml`): type-check and CDK synth for all
  environments on pull requests and pushes to `main`.
- **Deploy** (`.github/workflows/deploy.yml`): push to `main` deploys `dev`;
  `workflow_dispatch` can target `dev` / `qa` / `uat` / `prod` via GitHub
  Environments and `AISUITE_DEPLOY_ROLE_ARN`. Deploy also re-runs type-check
  and unit tests before assuming the OIDC role.

## CDK bootstrap

Modern stack synthesis needs a one-time `CDKToolkit` bootstrap in each AWS
account before `cdk deploy`. CI synth does not require it; deploy does.

Non-prod is bootstrapped. Prod still needs bootstrap before the first prod
deploy. Details: [`.github/oidc/README.md`](.github/oidc/README.md#cdk-bootstrap).

Agent/contributor working rules: see [`AGENTS.md`](AGENTS.md).
