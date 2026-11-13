import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as s3 from "aws-cdk-lib/aws-s3";
import { describe, expect, it } from "vitest";

import { PreProcessingLambdaConstruct } from "../src/constructs/pre-processing-lambda-construct";
import { getDeploymentConfig } from "../src/deployment-config";

describe("PreProcessingLambdaConstruct", () => {
  it("runs as a standard VPC Lambda invoked only by the manifest workflow", () => {
    const app = new cdk.App();
    const deploymentConfig = getDeploymentConfig("dev");
    const stack = new cdk.Stack(app, "PreProcessingTestStack", {
      env: deploymentConfig.awsEnvironment,
    });
    const vpc = new ec2.Vpc(stack, "Vpc", { maxAzs: 2, natGateways: 1 });
    new PreProcessingLambdaConstruct(stack, "PreProcessing", {
      appSecurityGroup: new ec2.SecurityGroup(stack, "Sg", { vpc }),
      deploymentConfig,
      documentsBucket: new s3.Bucket(stack, "Docs"),
      pipelineTempBucket: new s3.Bucket(stack, "Temp"),
      postProcessingBucket: new s3.Bucket(stack, "Post"),
      vpc,
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs("AWS::Lambda::CapacityProvider", 0);
    template.hasResourceProperties("AWS::Lambda::Function", {
      Architectures: ["arm64"],
      FunctionName: "aisuite-dev-pre-processing",
      ImageConfig: { Command: ["data_preprocessing.pre_processing.handler"] },
      PackageType: "Image",
      ReservedConcurrentExecutions: 5,
      Timeout: 900,
      VpcConfig: Match.objectLike({ SubnetIds: Match.anyValue() }),
    });
    template.hasResource("AWS::Logs::LogGroup", {
      DeletionPolicy: "Delete",
      Properties: {
        LogGroupName: "/aws/lambda/aisuite-dev-pre-processing",
        RetentionInDays: 30,
      },
    });
    const [preProcessingFunction] = Object.values(
      template.findResources("AWS::Lambda::Function", {
        Properties: { FunctionName: "aisuite-dev-pre-processing" },
      }),
    );
    expect(
      preProcessingFunction?.Properties?.Environment?.Variables,
    ).not.toHaveProperty("DB_SECRET_ARN");
    expect(JSON.stringify(template.toJSON())).not.toContain(
      "secretsmanager:GetSecretValue",
    );
    template.resourceCountIs("Custom::S3BucketNotifications", 1);
    template.hasResourceProperties("Custom::S3BucketNotifications", {
      NotificationConfiguration: {
        LambdaFunctionConfigurations: Match.arrayWith([
          Match.objectLike({
            Events: ["s3:ObjectCreated:*"],
            Filter: {
              Key: {
                FilterRules: [
                  { Name: "suffix", Value: ".pdf" },
                  { Name: "prefix", Value: "common/" },
                ],
              },
            },
          }),
        ]),
      },
    });
    template.resourceCountIs("AWS::Events::Rule", 0);
    template.resourceCountIs("AWS::Lambda::EventSourceMapping", 0);
  });
});
