import { afterEach, describe, expect, it } from "vitest";

import {
  AWS_ACCOUNT_IDS,
  DEPLOYMENT_ENVIRONMENT_NAMES,
  DEPLOYMENT_ENVIRONMENT_VPC_NAME,
  getDeploymentConfig,
  resolveAccountId,
} from "../src/deployment-config";

describe("deployment config", () => {
  afterEach(() => {
    delete process.env.DEPLOYMENT_OWNER;
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
});
