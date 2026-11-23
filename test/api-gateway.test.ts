import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";

import { AisuiteInfrastructureStack } from "../src/aisuite-infrastructure-stack";
import { STUB_VPC_CONTEXT_KEY } from "../src/constructs/networking-construct";
import { getDeploymentConfig } from "../src/deployment-config";

function synthesize(): Template {
  const app = new cdk.App({ context: { [STUB_VPC_CONTEXT_KEY]: true } });
  const config = getDeploymentConfig("dev");
  const stack = new AisuiteInfrastructureStack(app, config.stackName, {
    deploymentConfig: config,
    env: config.awsEnvironment,
    stackName: config.stackName,
  });

  return Template.fromStack(stack);
}

describe("api gateway + waf", () => {
  const template = synthesize();

  it("creates a VPC Link V2 in private subnets", () => {
    template.hasResourceProperties("AWS::ApiGatewayV2::VpcLink", {
      SubnetIds: Match.anyValue(),
      SecurityGroupIds: Match.anyValue(),
    });
  });

  it("routes methods through the VPC link to the internal ALB", () => {
    template.hasResourceProperties("AWS::ApiGateway::Method", {
      Integration: Match.objectLike({
        ConnectionType: "VPC_LINK",
        IntegrationTarget: Match.anyValue(),
      }),
    });
  });

  it("creates a regional web ACL associated with the stage", () => {
    template.hasResourceProperties("AWS::WAFv2::WebACL", { Scope: "REGIONAL" });
    template.resourceCountIs("AWS::WAFv2::WebACLAssociation", 1);
  });
});
