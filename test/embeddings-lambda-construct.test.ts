import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { describe, it } from "vitest";

import { EmbeddingsLambdaConstruct } from "../src/constructs/embeddings-lambda-construct";
import { getDeploymentConfig } from "../src/deployment-config";

describe("EmbeddingsLambdaConstruct", () => {
  it("runs as a standard VPC Lambda invoked once per RAG_Output JSON", () => {
    const app = new cdk.App();
    const deploymentConfig = getDeploymentConfig("prod");
    const stack = new cdk.Stack(app, "EmbeddingsTestStack", {
      env: deploymentConfig.awsEnvironment,
    });
    const vpc = new ec2.Vpc(stack, "Vpc", { maxAzs: 2, natGateways: 1 });
    new EmbeddingsLambdaConstruct(stack, "Embeddings", {
      appSecurityGroup: new ec2.SecurityGroup(stack, "Sg", { vpc }),
      dbSecret: new secretsmanager.Secret(stack, "Secret"),
      deploymentConfig,
      postProcessingBucket: new s3.Bucket(stack, "Post"),
      vpc,
    });

    const template = Template.fromStack(stack);
    template.resourceCountIs("AWS::Lambda::CapacityProvider", 0);
    template.hasResourceProperties("AWS::Lambda::Function", {
      Architectures: ["arm64"],
      FunctionName: "aisuite-prod-embeddings",
      ImageConfig: {
        Command: ["data_embeddings_storage.rag_process.handler"],
      },
      PackageType: "Image",
      ReservedConcurrentExecutions: 5,
      Timeout: 900,
      VpcConfig: Match.objectLike({ SubnetIds: Match.anyValue() }),
    });
    template.hasResourceProperties("Custom::S3BucketNotifications", {
      NotificationConfiguration: {
        LambdaFunctionConfigurations: [
          Match.objectLike({
            Events: ["s3:ObjectCreated:*"],
            Filter: {
              Key: {
                FilterRules: [
                  { Name: "suffix", Value: ".json" },
                  { Name: "prefix", Value: "RAG_Output/" },
                ],
              },
            },
          }),
        ],
      },
    });
    template.hasResourceProperties("AWS::Lambda::Permission", {
      Action: "lambda:InvokeFunction",
      Principal: "s3.amazonaws.com",
    });
    template.resourceCountIs("AWS::Events::Rule", 0);
  });
});
