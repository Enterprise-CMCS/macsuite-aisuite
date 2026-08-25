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
 * Optional CloudTamer cmscloud-vpn SG; attached to RDS so VPN/Zscaler can reach it.
 */
export const DEPLOYMENT_ENVIRONMENT_VPN_SECURITY_GROUP_ID: Partial<
  Record<DeploymentEnvironmentName, string>
> = {
  dev: "sg-0964f9710d200b1ac",
  qa: "sg-0964f9710d200b1ac",
  uat: "sg-049f5a4447ace5a2b",
  prod: "sg-0c723aa082515868d"
};

/** Per-environment ACM certificate ARNs for the ALB HTTPS listener. */
export const DEPLOYMENT_ENVIRONMENT_ALB_CERTIFICATE_ARN: Partial<
  Record<DeploymentEnvironmentName, string>
> = {
  dev: "arn:aws:acm:us-east-1:205501819586:certificate/5b20cd15-197a-4efc-b05c-0063a371ff30",
  qa: "arn:aws:acm:us-east-1:205501819586:certificate/cd2183fd-ba83-448c-99a6-521d00b3565f",
  uat: "arn:aws:acm:us-east-1:205501819586:certificate/56dab682-bd09-4873-b03e-db111b11ba51",
  prod: "arn:aws:acm:us-east-1:609425363642:certificate/89f2ea0b-f92b-473a-adf4-1c1629378868"
};

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
  DeployedAt: string
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
  /** CloudTamer cmscloud-vpn SG id when this stage allows VPN access to RDS. */
  vpnSecurityGroupId?: string;
  /** Optional ARN of an ACM certificate to use for the ALB HTTPS listener. */
  albCertificateArn?: string;
  /** Name of the central S3 bucket that receives server access logs (managed externally). */
  accessLogsBucketName: string;
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

  const deployedAt = process.env.DEPLOYMENT_TIMESTAMP ?? new Date().toISOString();
  return {
    accountTier,
    awsEnvironment: {
      account: resolveAccountId(accountTier),
      region: DEFAULT_REGION,
    },
    name: environmentName,
    protectedEnvironment,
    stackName: `${APPLICATION_NAME}-${environmentName}-infrastructure`,
    tags: {
      Application: APPLICATION_NAME,
      AwsAccountTier: accountTier,
      Environment: environmentName,
      ManagedBy: MANAGED_BY,
      Owner: process.env.DEPLOYMENT_OWNER ?? DEFAULT_OWNER,
      Protected: String(protectedEnvironment),
      DeployedAt: deployedAt
    },
    vpcName: DEPLOYMENT_ENVIRONMENT_VPC_NAME[environmentName],
    vpnSecurityGroupId: DEPLOYMENT_ENVIRONMENT_VPN_SECURITY_GROUP_ID[environmentName],
    albCertificateArn: DEPLOYMENT_ENVIRONMENT_ALB_CERTIFICATE_ARN[environmentName],
    accessLogsBucketName: `cms-cloud-${resolveAccountId(accountTier)}-${DEFAULT_REGION}-access-logs`,
  };
}

export function isDeploymentEnvironmentName(
  environmentName: string,
): environmentName is DeploymentEnvironmentName {
  return DEPLOYMENT_ENVIRONMENT_NAMES.includes(
    environmentName as DeploymentEnvironmentName,
  );
}