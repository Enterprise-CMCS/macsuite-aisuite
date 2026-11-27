import * as cdk from "aws-cdk-lib";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import type * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as logs from "aws-cdk-lib/aws-logs";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { Construct } from "constructs";

import { APPLICATION_NAME, type DeploymentConfig } from "../deployment-config";

export const API_STAGE_NAME = "v1";

/** Exact API Gateway stage-prefixed path exempt from WAF's 8 KiB body-size rule. */
export const MANIFEST_UPLOAD_PATH = `/${API_STAGE_NAME}/manifest`;

/** Requests per 5 minutes per source IP before WAF blocks. */
export const WAF_RATE_LIMIT_PER_IP = 2000;

const WAF_COMMON_RULE_SET = "AWSManagedRulesCommonRuleSet";
const WAF_BODY_SIZE_RULE_NAME = "SizeRestrictions_BODY";
const WAF_BODY_SIZE_LABEL =
  "awswaf:managed:aws:core-rule-set:SizeRestrictions_Body";

const ADDITIONAL_AWS_MANAGED_RULE_GROUPS = [
  "AWSManagedRulesKnownBadInputsRuleSet",
  "AWSManagedRulesAmazonIpReputationList",
] as const;

export interface ApiGatewayConstructProps {
  deploymentConfig: DeploymentConfig;
  loadBalancer: elbv2.IApplicationLoadBalancer;
  loadBalancerSecurityGroup: ec2.ISecurityGroup;
  vpc: ec2.IVpc;
}

/**
 * Regional REST API (WAF-protected) that proxies to the internal ALB through an
 * API Gateway VPC Link V2.
 */
export class ApiGatewayConstruct extends Construct {
  public readonly api: apigateway.RestApi;
  public readonly vpcLink: apigatewayv2.CfnVpcLink;
  public readonly webAcl: wafv2.CfnWebACL;

  public constructor(
    scope: Construct,
    id: string,
    props: ApiGatewayConstructProps,
  ) {
    super(scope, id);

    const { name, protectedEnvironment } = props.deploymentConfig;
    const baseName = `${APPLICATION_NAME}-${name}-rag-api`;
    const removalPolicy = protectedEnvironment
      ? cdk.RemovalPolicy.RETAIN
      : cdk.RemovalPolicy.DESTROY;

    const vpcLinkSecurityGroup = new ec2.SecurityGroup(
      this,
      "VpcLinkSecurityGroup",
      {
        allowAllOutbound: false,
        description: "AISuite API Gateway VPC Link V2; egress to the internal ALB.",
        vpc: props.vpc,
      },
    );
    vpcLinkSecurityGroup.addEgressRule(
      props.loadBalancerSecurityGroup,
      ec2.Port.tcp(443),
      "HTTPS to the internal ALB",
    );
    props.loadBalancerSecurityGroup.addIngressRule(
      vpcLinkSecurityGroup,
      ec2.Port.tcp(443),
      "HTTPS from the API Gateway VPC Link",
    );
    // The ALB security group has no outbound rules by default; the VPC link
    // SG ingress above is what permits API Gateway traffic.

    const { subnetIds } = props.vpc.selectSubnets({
      subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
    });

    this.vpcLink = new apigatewayv2.CfnVpcLink(this, "VpcLink", {
      name: `${baseName}-vpclink`,
      securityGroupIds: [vpcLinkSecurityGroup.securityGroupId],
      subnetIds,
    });

    const accessLogGroup = new logs.LogGroup(this, "AccessLogGroup", {
      logGroupName: `/apigateway/${baseName}/access`,
      removalPolicy,
      retention: logs.RetentionDays.ONE_MONTH,
    });

    // API Gateway's CloudWatch Logs role is an account-level setting. Each
    // environment owns a separately named role (retained so deleting one
    // stage's stack cannot break logging for stages sharing the account).
    const cloudWatchRole = new iam.Role(this, "ApiGatewayCloudWatchRole", {
      assumedBy: new iam.ServicePrincipal("apigateway.amazonaws.com"),
      description: `API Gateway CloudWatch Logs role for ${name}.`,
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AmazonAPIGatewayPushToCloudWatchLogs",
        ),
      ],
    });
    cloudWatchRole.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
    const apiAccount = new apigateway.CfnAccount(this, "ApiAccount", {
      cloudWatchRoleArn: cloudWatchRole.roleArn,
    });
    apiAccount.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    this.api = new apigateway.RestApi(this, "Api", {
      cloudWatchRole: false,
      deploy: true,
      deployOptions: {
        accessLogDestination: new apigateway.LogGroupLogDestination(
          accessLogGroup,
        ),
        accessLogFormat: apigateway.AccessLogFormat.jsonWithStandardFields(),
        loggingLevel: apigateway.MethodLoggingLevel.ERROR,
        methodOptions: {
          "/*/*": {
            throttlingBurstLimit: 100,
            throttlingRateLimit: 50,
          },
        },
        stageName: API_STAGE_NAME,
      },
      description: "AISuite RAG API fronting the internal ALB via VPC Link V2.",
      endpointConfiguration: { types: [apigateway.EndpointType.REGIONAL] },
      restApiName: baseName,
    });

    this.api.deploymentStage.node.addDependency(apiAccount);

    const albHost = props.deploymentConfig.albHostName;
    const integrationUri = `https://${albHost}:443/{proxy}`;

    const proxyResource = this.api.root.addResource("{proxy+}");
    const methodOptions: apigateway.MethodOptions = {
      authorizationType: apigateway.AuthorizationType.IAM,
    };
    const proxyMethodOptions: apigateway.MethodOptions = {
      ...methodOptions,
      requestParameters: { "method.request.path.proxy": true },
    };
    const integrationOptions: apigateway.IntegrationOptions = {
      requestParameters: {
        "integration.request.path.proxy": "method.request.path.proxy",
      },
    };

    for (const target of [this.api.root, proxyResource]) {
      const isProxy = target === proxyResource;
      const method = target.addMethod(
        "ANY",
        new apigateway.HttpIntegration(
          isProxy ? integrationUri : `https://${albHost}:443/`,
          {
            httpMethod: "ANY",
            options: isProxy ? integrationOptions : {},
            proxy: true,
          },
        ),
        isProxy ? proxyMethodOptions : methodOptions,
      );

      // VPC Link V2 + ALB target is not modelled by the L2 constructs yet.
      const cfnMethod = method.node.defaultChild as apigateway.CfnMethod;
      cfnMethod.addPropertyOverride("Integration.ConnectionType", "VPC_LINK");
      cfnMethod.addPropertyOverride(
        "Integration.ConnectionId",
        this.vpcLink.attrVpcLinkId,
      );
      cfnMethod.addPropertyOverride(
        "Integration.IntegrationTarget",
        props.loadBalancer.loadBalancerArn,
      );
    }

    this.webAcl = new wafv2.CfnWebACL(this, "WebAcl", {
      defaultAction: { allow: {} },
      description: "AISuite API Gateway protection.",
      name: `${baseName}-waf`,
      rules: [
        {
          action: { block: {} },
          name: "RateLimitPerIp",
          priority: 0,
          statement: {
            rateBasedStatement: {
              aggregateKeyType: "IP",
              limit: WAF_RATE_LIMIT_PER_IP,
            },
          },
          visibilityConfig: visibility("RateLimitPerIp"),
        },
        {
          name: WAF_COMMON_RULE_SET,
          overrideAction: { none: {} },
          priority: 1,
          statement: {
            managedRuleGroupStatement: {
              name: WAF_COMMON_RULE_SET,
              // Count SizeRestrictions_BODY so the scoped rule below can exempt
              // POST /v1/manifest while keeping all other CRS protections active.
              ruleActionOverrides: [
                {
                  actionToUse: { count: {} },
                  name: WAF_BODY_SIZE_RULE_NAME,
                },
              ],
              vendorName: "AWS",
            },
          },
          visibilityConfig: visibility(WAF_COMMON_RULE_SET),
        },
        {
          action: { block: {} },
          name: "EnforceBodySizeOutsideManifestUpload",
          priority: 2,
          statement: {
            andStatement: {
              statements: [
                {
                  labelMatchStatement: {
                    key: WAF_BODY_SIZE_LABEL,
                    scope: "LABEL",
                  },
                },
                {
                  notStatement: {
                    statement: {
                      andStatement: {
                        statements: [
                          {
                            byteMatchStatement: {
                              fieldToMatch: { method: {} },
                              positionalConstraint: "EXACTLY",
                              searchString: "POST",
                              textTransformations: [
                                { priority: 0, type: "NONE" },
                              ],
                            },
                          },
                          {
                            byteMatchStatement: {
                              fieldToMatch: { uriPath: {} },
                              positionalConstraint: "EXACTLY",
                              searchString: MANIFEST_UPLOAD_PATH,
                              textTransformations: [
                                { priority: 0, type: "NONE" },
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
          visibilityConfig: visibility("EnforceBodySizeOutsideManifestUpload"),
        },
        ...ADDITIONAL_AWS_MANAGED_RULE_GROUPS.map((ruleName, index) => ({
          name: ruleName,
          overrideAction: { none: {} },
          priority: index + 3,
          statement: {
            managedRuleGroupStatement: {
              name: ruleName,
              vendorName: "AWS",
            },
          },
          visibilityConfig: visibility(ruleName),
        })),
      ],
      scope: "REGIONAL",
      visibilityConfig: visibility(`${baseName}-waf`),
    });

    const stageArn = cdk.Stack.of(this).formatArn({
      account: "",
      resource: "/restapis",
      resourceName: `${this.api.restApiId}/stages/${this.api.deploymentStage.stageName}`,
      service: "apigateway",
    });
    const association = new wafv2.CfnWebACLAssociation(this, "WebAclAssociation", {
      resourceArn: stageArn,
      webAclArn: this.webAcl.attrArn,
    });
    association.node.addDependency(this.api.deploymentStage);

    // WAF requires the log group name to start with `aws-waf-logs-`.
    const wafLogGroup = new logs.LogGroup(this, "WafLogGroup", {
      logGroupName: `aws-waf-logs-${baseName}`,
      removalPolicy,
      retention: logs.RetentionDays.ONE_MONTH,
    });
    new wafv2.CfnLoggingConfiguration(this, "WafLogging", {
      logDestinationConfigs: [
        cdk.Stack.of(this).formatArn({
          arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
          resource: "log-group",
          resourceName: wafLogGroup.logGroupName,
          service: "logs",
        }),
      ],
      resourceArn: this.webAcl.attrArn,
    }).node.addDependency(wafLogGroup);
  }
}

function visibility(
  metricName: string,
): wafv2.CfnWebACL.VisibilityConfigProperty {
  return {
    cloudWatchMetricsEnabled: true,
    metricName,
    sampledRequestsEnabled: true,
  };
}
