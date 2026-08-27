import { afterEach, describe, expect, it } from "vitest";

import {
  AWS_ACCOUNT_IDS,
  DEFAULT_REGION,
  DEPLOYMENT_ENVIRONMENT_ALB_CERTIFICATE_ARN,
  DEPLOYMENT_ENVIRONMENT_NAMES,
  DEPLOYMENT_ENVIRONMENT_VPC_NAME,
  DEPLOYMENT_ENVIRONMENT_VPN_SECURITY_GROUP_ID,
  getDeploymentConfig,
  resolveAccountId,
} from "../src/deployment-config";

describe("deployment config", () => {
  afterEach(() => {
    delete process.env.DEPLOYMENT_OWNER;
    delete process.env.DEPLOYMENT_TIMESTAMP;
    delete process.env.AISUITE_NONPROD_ACCOUNT_ID;
    delete process.env.AISUITE_PROD_ACCOUNT_ID;
    delete process.env.DEFAULT_REGION;
  });

  it("builds a namespaced stack config for a known environment", () => {
    const config = getDeploymentConfig("dev");

    expect(config.name).toBe("dev");
    expect(config.accountTier).toBe("nonprod");
    expect(config.protectedEnvironment).toBe(false);
    expect(config.stackName).toBe("aisuite-dev-infrastructure");
    expect(config.vpcName).toBe("aisuite-east-dev");
    expect(config.vpnSecurityGroupId).toBe("sg-0964f9710d200b1ac");
    expect(config.awsEnvironment.account).toMatch(/^\d{12}$/);
    expect(config.awsEnvironment.region).toBeTruthy();
    expect(config.tags.Application).toBe("aisuite");
    expect(config.tags.Environment).toBe("dev");
  });

  it("routes prod to the prod account tier and marks it protected", () => {
    const config = getDeploymentConfig("prod");

    expect(config.accountTier).toBe("prod");
    expect(config.protectedEnvironment).toBe(true);
    expect(config.stackName).toBe("aisuite-prod-infrastructure");
    expect(config.vpcName).toBe("aisuite-east-prod");
  });

  it("maps each app stage to the CloudTamer VPC Name tag", () => {
    expect(DEPLOYMENT_ENVIRONMENT_VPC_NAME).toEqual({
      dev: "aisuite-east-dev",
      qa: "aisuite-east-qa",
      uat: "aisuite-east-test",
      prod: "aisuite-east-prod",
    });

    for (const environmentName of DEPLOYMENT_ENVIRONMENT_NAMES) {
      const config = getDeploymentConfig(environmentName);
      expect(config.vpcName).toBe(
        DEPLOYMENT_ENVIRONMENT_VPC_NAME[environmentName],
      );
    }
  });

  it("attaches CloudTamer VPN access SG for each environment", () => {
    expect(DEPLOYMENT_ENVIRONMENT_VPN_SECURITY_GROUP_ID).toEqual({
      dev: "sg-0964f9710d200b1ac",
      qa: "sg-0964f9710d200b1ac",
      uat: "sg-049f5a4447ace5a2b",
      prod: "sg-0c723aa082515868d",
    });

    for (const environmentName of DEPLOYMENT_ENVIRONMENT_NAMES) {
      expect(getDeploymentConfig(environmentName).vpnSecurityGroupId).toBe(
        DEPLOYMENT_ENVIRONMENT_VPN_SECURITY_GROUP_ID[environmentName],
      );
    }
  });

  it("maps an ACM certificate ARN for every environment", () => {
    expect(DEPLOYMENT_ENVIRONMENT_ALB_CERTIFICATE_ARN).toEqual({
      dev: "arn:aws:acm:us-east-1:205501819586:certificate/5b20cd15-197a-4efc-b05c-0063a371ff30",
      qa: "arn:aws:acm:us-east-1:205501819586:certificate/cd2183fd-ba83-448c-99a6-521d00b3565f",
      uat: "arn:aws:acm:us-east-1:205501819586:certificate/56dab682-bd09-4873-b03e-db111b11ba51",
      prod: "arn:aws:acm:us-east-1:609425363642:certificate/89f2ea0b-f92b-473a-adf4-1c1629378868",
    });

    for (const environmentName of DEPLOYMENT_ENVIRONMENT_NAMES) {
      const certificateArn =
        DEPLOYMENT_ENVIRONMENT_ALB_CERTIFICATE_ARN[environmentName];
      expect(certificateArn).toMatch(/^arn:aws:acm:/);
      expect(getDeploymentConfig(environmentName).albCertificateArn).toBe(
        certificateArn,
      );
    }
  });

  it("names the central access-logs bucket per account and region", () => {
    for (const environmentName of DEPLOYMENT_ENVIRONMENT_NAMES) {
      const config = getDeploymentConfig(environmentName);
      expect(config.accessLogsBucketName).toBe(
        `cms-cloud-${config.awsEnvironment.account}-${DEFAULT_REGION}-access-logs`,
      );
    }
  });

  it("covers every declared deployment environment name", () => {
    for (const environmentName of DEPLOYMENT_ENVIRONMENT_NAMES) {
      const config = getDeploymentConfig(environmentName);
      expect(config.stackName).toBe(
        `aisuite-${environmentName}-infrastructure`,
      );
    }
  });

  it("rejects unknown environments", () => {
    expect(() => getDeploymentConfig("val")).toThrow(
      /Unsupported deployment environment/,
    );
  });

  it("lets env overrides win over configured account IDs", () => {
    process.env.AISUITE_NONPROD_ACCOUNT_ID = "111111111111";

    expect(resolveAccountId("nonprod")).toBe("111111111111");
  });

  it("fails fast when an override is not a 12-digit account id", () => {
    process.env.AISUITE_PROD_ACCOUNT_ID = "not-an-account";

    expect(() => resolveAccountId("prod")).toThrow();
  });

  it("exposes configured CloudTamer account ids as 12-digit strings", () => {
    expect(AWS_ACCOUNT_IDS.nonprod).toBe("205501819586");
    expect(AWS_ACCOUNT_IDS.prod).toBe("609425363642");
  });

  it("uses DEPLOYMENT_TIMESTAMP when set", () => {
    process.env.DEPLOYMENT_TIMESTAMP = "2026-08-24T17:23:17-04:00";

    expect(getDeploymentConfig("dev").tags.DeployedAt).toBe(
      "2026-08-24T17:23:17-04:00",
    );
  });

  it("falls back when DEPLOYMENT_TIMESTAMP is blank", () => {
    process.env.DEPLOYMENT_TIMESTAMP = "   ";

    const deployedAt = getDeploymentConfig("dev").tags.DeployedAt;
    expect(deployedAt).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });
});
