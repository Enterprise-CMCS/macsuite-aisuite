import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";

import { AisuiteInfrastructureStack } from "../src/aisuite-infrastructure-stack";
import { STUB_VPC_CONTEXT_KEY } from "../src/constructs/networking-construct";
import { getDeploymentConfig } from "../src/deployment-config";

describe("AisuiteInfrastructureStack", () => {
  it("synthesizes with an explicit stack name and environment outputs", () => {
    const app = new cdk.App({ context: { [STUB_VPC_CONTEXT_KEY]: true } });
    const config = getDeploymentConfig("dev");
    const stack = new AisuiteInfrastructureStack(app, config.stackName, {
      deploymentConfig: config,
      env: config.awsEnvironment,
      stackName: config.stackName,
    });

    const template = Template.fromStack(stack);

    expect(stack.stackName).toBe("aisuite-dev-infrastructure");
    template.hasOutput("DeploymentEnvironment", { Value: "dev" });
    template.hasOutput("ProtectedEnvironment", { Value: "false" });
  });
});
