# macsuite-aisuite

Greenfield TypeScript AWS CDK application for OY2-40308.

Logical stages `dev` / `qa` / `uat` map to the shared non-prod AWS account;
`prod` maps to the prod account. GitHub Actions assumes an OIDC deploy role per
account — see [`.github/oidc/README.md`](.github/oidc/README.md).

## Architecture

Each environment deploys one stack (`aisuite-{env}-infrastructure`): an
imported CloudTamer VPC hosts an internal ALB and ECS Fargate RAG API, on-demand
ECS batch tasks (pre-processing, RAG process, DB bootstrap), and private RDS
Postgres with pgvector. S3, Secrets Manager, ECR, and Bedrock sit outside the
VPC but in the same account/region.

![AISuite Phase 2 architecture](docs/aisuite-phase2-architecture.drawio.png)

*Alt text: AWS reference architecture for AISuite Phase 2 VPC, ECS, RDS, S3, and Bedrock.*
Open or edit the diagram in [diagrams.net](https://app.diagrams.net/) by
loading `docs/aisuite-phase2-architecture.drawio.png` (PNG with embedded draw.io XML).

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
