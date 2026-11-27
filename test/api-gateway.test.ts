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
      AuthorizationType: "AWS_IAM",
      Integration: Match.objectLike({
        ConnectionType: "VPC_LINK",
        IntegrationTarget: Match.anyValue(),
      }),
    });
  });

  it("requires IAM authorization on both root and proxy methods", () => {
    const methods = Object.values(
      template.findResources("AWS::ApiGateway::Method"),
    );

    expect(methods).toHaveLength(2);
    expect(
      methods.map(({ Properties }) => Properties.AuthorizationType),
    ).toEqual(["AWS_IAM", "AWS_IAM"]);
  });

  it("creates a regional web ACL associated with the stage", () => {
    template.hasResourceProperties("AWS::WAFv2::WebACL", { Scope: "REGIONAL" });
    template.resourceCountIs("AWS::WAFv2::WebACLAssociation", 1);
  });

  it("exempts only POST /v1/manifest from SizeRestrictions_BODY while keeping all other WAF rules active", () => {
    template.hasResourceProperties("AWS::WAFv2::WebACL", {
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: "AWSManagedRulesCommonRuleSet",
          OverrideAction: { None: {} },
          Priority: 1,
          Statement: {
            ManagedRuleGroupStatement: {
              Name: "AWSManagedRulesCommonRuleSet",
              RuleActionOverrides: [
                {
                  ActionToUse: { Count: {} },
                  Name: "SizeRestrictions_BODY",
                },
              ],
              VendorName: "AWS",
            },
          },
        }),
        Match.objectLike({
          Action: { Block: {} },
          Name: "EnforceBodySizeOutsideManifestUpload",
          Priority: 2,
          Statement: {
            AndStatement: {
              Statements: [
                {
                  LabelMatchStatement: {
                    Key: "awswaf:managed:aws:core-rule-set:SizeRestrictions_Body",
                    Scope: "LABEL",
                  },
                },
                {
                  NotStatement: {
                    Statement: {
                      AndStatement: {
                        Statements: [
                          {
                            ByteMatchStatement: {
                              FieldToMatch: { Method: {} },
                              PositionalConstraint: "EXACTLY",
                              SearchString: "POST",
                              TextTransformations: [
                                { Priority: 0, Type: "NONE" },
                              ],
                            },
                          },
                          {
                            ByteMatchStatement: {
                              FieldToMatch: { UriPath: {} },
                              PositionalConstraint: "EXACTLY",
                              SearchString: "/v1/manifest",
                              TextTransformations: [
                                { Priority: 0, Type: "NONE" },
                              ],
                            },
                          },
                        ],
                      },
                    },
                  },
                },
              ],
            },
          },
        }),
        Match.objectLike({
          Name: "AWSManagedRulesKnownBadInputsRuleSet",
          OverrideAction: { None: {} },
          Priority: 3,
        }),
        Match.objectLike({
          Name: "AWSManagedRulesAmazonIpReputationList",
          OverrideAction: { None: {} },
          Priority: 4,
        }),
      ]),
    });
  });
});
