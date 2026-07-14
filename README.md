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

Agent/contributor working rules: see [`AGENTS.md`](AGENTS.md).
