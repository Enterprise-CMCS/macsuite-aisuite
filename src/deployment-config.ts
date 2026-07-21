import type * as cdk from "aws-cdk-lib";

export const DEPLOYMENT_ENVIRONMENT_NAMES = [
  "dev",
  "qa",
  "uat",
  "prod",
] as const;
export type DeploymentEnvironmentName =
  (typeof DEPLOYMENT_ENVIRONMENT_NAMES)[number];

/** AWS account isolation tiers available to AISuite (two physical accounts). */
export const AWS_ACCOUNT_TIERS = ["nonprod", "prod"] as const;
export type AwsAccountTier = (typeof AWS_ACCOUNT_TIERS)[number];

/**
 * Logical stages share the non-prod account; only `prod` deploys into the prod
 * account. Stack names and tags keep stages isolated within a shared account.
 */
export const DEPLOYMENT_ENVIRONMENT_ACCOUNT_TIER = {
  dev: "nonprod",
  qa: "nonprod",
  uat: "nonprod",
  prod: "prod",
} as const satisfies Record<DeploymentEnvironmentName, AwsAccountTier>;

/**
 * CloudTamer-provisioned VPC Name tags (aisuite-east-*). App stages map 1:1
 * except `uat` → `aisuite-east-test` (CloudTamer has no uat VPC name).
 */
export const DEPLOYMENT_ENVIRONMENT_VPC_NAME = {
  dev: "aisuite-east-dev",
  qa: "aisuite-east-qa",
  uat: "aisuite-east-test",
  prod: "aisuite-east-prod",
} as const satisfies Record<DeploymentEnvironmentName, string>;

/**
 * Explicit AISuite AWS account IDs (CloudTamer aisuite-non-prod / aisuite-prod).
 * Runtime env overrides (`AISUITE_NONPROD_ACCOUNT_ID` /
 * `AISUITE_PROD_ACCOUNT_ID`) win when set (CI / local override).
 */
export const AWS_ACCOUNT_IDS: Record<AwsAccountTier, string> = {
  nonprod: "205501819586",
  prod: "609425363642",
};

const ACCOUNT_ID_ENV_KEYS = {
  nonprod: "AISUITE_NONPROD_ACCOUNT_ID",
  prod: "AISUITE_PROD_ACCOUNT_ID",
} as const satisfies Record<AwsAccountTier, string>;

export const DEFAULT_REGION = "us-east-1";

export const APPLICATION_NAME = "aisuite";
export const MANAGED_BY = "AWS CDK";
export const WORK_ITEM = "OY2-40308";

/**
 * Fallback owner when `DEPLOYMENT_OWNER` is not exported. Override per repo/team
 * (e.g. a distribution list or team slug) rather than a single person.
 */
export const DEFAULT_OWNER = "AISuite Platform Team";

/**
 * Standard tag taxonomy applied to every stack. Keys are intentionally fixed so
 * that all environments and any future stacks share one consistent cost- and
 * ownership-tracking vocabulary.
 */
export interface StandardTags {
  [tagKey: string]: string;
  Application: string;
  AwsAccountTier: AwsAccountTier;
  Environment: DeploymentEnvironmentName;
  ManagedBy: string;
  Owner: string;
  Protected: string;
}

export interface DeploymentConfig {
  accountTier: AwsAccountTier;
  awsEnvironment: cdk.Environment;
  name: DeploymentEnvironmentName;
  protectedEnvironment: boolean;
  stackName: string;
  tags: StandardTags;
  /** CloudTamer VPC Name tag for this stage (use with `ec2.Vpc.fromLookup`). */
  vpcName: (typeof DEPLOYMENT_ENVIRONMENT_VPC_NAME)[DeploymentEnvironmentName];
}

const ACCOUNT_ID_PATTERN = /^\d{12}$/;

export function resolveAccountId(tier: AwsAccountTier): string {
  const accountId =
    process.env[ACCOUNT_ID_ENV_KEYS[tier]] ?? AWS_ACCOUNT_IDS[tier];
  if (!ACCOUNT_ID_PATTERN.test(accountId)) {
    throw new Error(
      `Update AWS_ACCOUNT_IDS in src/deployment-config.ts or export ${ACCOUNT_ID_ENV_KEYS[tier]}.`,
    );
  }

  return accountId;
}

export function getDeploymentConfig(
  environmentName: string,
): DeploymentConfig {
  if (!isDeploymentEnvironmentName(environmentName)) {
    const expected = DEPLOYMENT_ENVIRONMENT_NAMES.join(", ");
    throw new Error(
      `Unsupported deployment environment "${environmentName}". Expected one of: ${expected}.`,
    );
  }

  const accountTier = DEPLOYMENT_ENVIRONMENT_ACCOUNT_TIER[environmentName];
  const protectedEnvironment = environmentName !== "dev";

  return {
    accountTier,
    awsEnvironment: {
      account: resolveAccountId(accountTier),
      region: DEFAULT_REGION,
    },
    name: environmentName,
    protectedEnvironment,
    stackName: `aisuite-${environmentName}-infrastructure`,
    tags: {
      Application: APPLICATION_NAME,
      AwsAccountTier: accountTier,
      Environment: environmentName,
      ManagedBy: MANAGED_BY,
      Owner: process.env.DEPLOYMENT_OWNER ?? DEFAULT_OWNER,
      Protected: String(protectedEnvironment),
    },
    vpcName: DEPLOYMENT_ENVIRONMENT_VPC_NAME[environmentName],
  };
}

export function isDeploymentEnvironmentName(
  environmentName: string,
): environmentName is DeploymentEnvironmentName {
  return DEPLOYMENT_ENVIRONMENT_NAMES.includes(
    environmentName as DeploymentEnvironmentName,
  );
}
