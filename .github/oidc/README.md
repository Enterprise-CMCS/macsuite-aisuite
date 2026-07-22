# GitHub Actions OIDC for AISuite

Wire GitHub Environments to the two AISuite AWS accounts so deploy workflows can
assume an IAM role without static keys.

## Account model

| GitHub Environment | AWS account tier | Deploy stack pattern |
| --- | --- | --- |
| `dev` | non-prod | `aisuite-dev-infrastructure` |
| `qa` | non-prod | `aisuite-qa-infrastructure` |
| `uat` | non-prod | `aisuite-uat-infrastructure` |
| `prod` | prod | `aisuite-prod-infrastructure` |

One OIDC deploy role is created **per AWS account**. Non-prod trusts `dev` /
`qa` / `uat`. Prod trusts only `prod`.

Role permissions follow the org pattern used by Bigmac: CMS permissions boundary
+ `ADO-Restriction-Policy` + `CMSApprovedAWSServices` +
`AdministratorAccess`. That is enough for CDK deploy without inventing a
narrower custom policy on top of CMS guardrails.

## Prerequisites

1. Non-prod and prod AISuite accounts exist and you can assume a CloudTamer /
   admin role in each.
2. GitHub Environments exist on
   [Enterprise-CMCS/macsuite-aisuite](https://github.com/Enterprise-CMCS/macsuite-aisuite):
   `dev`, `qa`, `uat`, `prod`. Protect `qa` / `uat` / `prod` with required
   reviewers as needed.
3. Local tools: AWS CLI v2, credentials for the target account.

## Deploy the OIDC stack

From the repo root, signed into the **non-prod** account:

```bash
aws cloudformation deploy \
  --stack-name aisuite-github-oidc \
  --template-file .github/oidc/github-actions-oidc-template.yml \
  --parameter-overrides file://.github/oidc/nonprod.json \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

Then signed into the **prod** account:

```bash
aws cloudformation deploy \
  --stack-name aisuite-github-oidc \
  --template-file .github/oidc/github-actions-oidc-template.yml \
  --parameter-overrides file://.github/oidc/prod.json \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

Capture the role ARN from each stack:

```bash
aws cloudformation describe-stacks \
  --stack-name aisuite-github-oidc \
  --query "Stacks[0].Outputs[?OutputKey=='DeployRoleArn'].OutputValue" \
  --output text \
  --region us-east-1
```

### If the OIDC provider already exists

Set in the parameter file (or CLI overrides):

- `CreateOidcProvider=false`
- `ExistingOidcProviderArn=arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com`

## Configure GitHub

### Repository / environment variables

Set these so CDK synth pins the correct account before assume-role:

| Name | Scope | Value |
| --- | --- | --- |
| `AISUITE_NONPROD_ACCOUNT_ID` | repository (optional) | `205501819586` — already pinned in `src/deployment-config.ts` |
| `AISUITE_PROD_ACCOUNT_ID` | repository (optional) | `609425363642` — already pinned in `src/deployment-config.ts` |
| `AISUITE_DEPLOY_REGION` | repository (optional) | `us-east-1` (default) |

Set the assume-role ARN **per GitHub Environment** (same ARN for all non-prod
environments):

| GitHub Environment | Variable | Value |
| --- | --- | --- |
| `dev` | `AISUITE_DEPLOY_ROLE_ARN` | non-prod role ARN |
| `qa` | `AISUITE_DEPLOY_ROLE_ARN` | non-prod role ARN |
| `uat` | `AISUITE_DEPLOY_ROLE_ARN` | non-prod role ARN |
| `prod` | `AISUITE_DEPLOY_ROLE_ARN` | prod role ARN |

Also update `AWS_ACCOUNT_IDS` in `src/deployment-config.ts` once IDs are known
so local synth matches CI without relying on env exports alone.

### Environment protection

- `dev`: no required reviewers (auto-deploy from `main` is OK).
- `qa` / `uat` / `prod`: required reviewers (and optional wait timers).

Subject claims require the workflow `environment:` key to match; that is already
set in `.github/workflows/deploy.yml`.

## CDK bootstrap

Modern stack synthesis (`newStyleStackSynthesis`) needs a one-time
`CDKToolkit` bootstrap in each AWS account/region before `cdk deploy`.
CI synth does not require it; deploy does.

Non-prod is bootstrapped. Prod still needs bootstrap before the first prod
deploy. Use the CMS path-qualified permissions boundary and
`AdministratorAccess` as the CloudFormation execution policy. Same-account
GitHub OIDC deploy does not need `--trust`.

## Validate

1. Manually dispatch **AISuite Deploy** for `dev`.
2. Confirm the job assumes the non-prod role and CDK deploy succeeds (or shows
   an empty/minimal stack change).
3. Repeat for `prod` after protection rules are in place.

## Files

- `github-actions-oidc-template.yml` — shared CloudFormation template
- `nonprod.json` — parameters for the shared non-prod role
- `prod.json` — parameters for the prod-only role
