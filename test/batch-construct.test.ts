import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { describe, expect, it } from "vitest";

import { BatchConstruct } from "../src/constructs/batch-construct";
import { getDeploymentConfig } from "../src/deployment-config";

function synthesize(): {
  batch: BatchConstruct;
  template: Template;
} {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, "BatchTestStack", {
    env: getDeploymentConfig("dev").awsEnvironment,
  });
  const vpc = new ec2.Vpc(stack, "Vpc", {
    maxAzs: 2,
    natGateways: 1,
  });
  const appSecurityGroup = new ec2.SecurityGroup(stack, "AppSecurityGroup", {
    vpc,
  });
  const taskRole = new iam.Role(stack, "TaskRole", {
    assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
  });

  const batch = new BatchConstruct(stack, "Batch", {
    appSecurityGroup,
    dbSecret: new secretsmanager.Secret(stack, "AppDbSecret"),
    deploymentConfig: getDeploymentConfig("dev"),
    documentsBucket: new s3.Bucket(stack, "DocumentsBucket"),
    pipelineCodeBucket: new s3.Bucket(stack, "PipelineCodeBucket"),
    pipelineTempBucket: new s3.Bucket(stack, "PipelineTempBucket"),
    postProcessingBucket: new s3.Bucket(stack, "PostProcessingBucket"),
    taskRole,
    vpc,
  });

  return { batch, template: Template.fromStack(stack) };
}

describe("BatchConstruct", () => {
  it("creates two on-demand Fargate task definitions with expected families", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::ECS::TaskDefinition", 2);
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Command: ["python", "-m", "data_preprocessing.pre_processing"],
        }),
      ]),
      Family: "aisuite-dev-pre-processing",
      NetworkMode: "awsvpc",
      RequiresCompatibilities: ["FARGATE"],
    });
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Command: ["python", "-m", "data_embeddings_storage.rag_process"],
        }),
      ]),
      Family: "aisuite-dev-rag-process",
      NetworkMode: "awsvpc",
      RequiresCompatibilities: ["FARGATE"],
    });
  });

  it("uses the shared image and API environment contract", () => {
    const { template } = synthesize();
    const taskDefinitions = Object.values(
      template.findResources("AWS::ECS::TaskDefinition"),
    );

    for (const taskDefinition of taskDefinitions) {
      const [container] = taskDefinition.Properties
        ?.ContainerDefinitions as Array<{
        Environment: Array<{ Name: string; Value: unknown }>;
        Image: unknown;
      }>;
      const environmentNames = container?.Environment.map(({ Name }) => Name);

      expect(environmentNames).toEqual(
        expect.arrayContaining([
          "DOCUMENTS_BUCKET",
          "POST_PROCESSING_BUCKET",
          "PIPELINE_CODE_BUCKET",
          "PIPELINE_TEMP_BUCKET",
          "DB_SECRET_ARN",
          "BEDROCK_MODEL_ID",
          "BEDROCK_EMBED_MODEL_ID",
          "AWS_REGION",
        ]),
      );
      expect(container?.Image).toEqual(
        expect.objectContaining({ "Fn::Join": expect.any(Array) }),
      );
    }

    template.resourceCountIs("AWS::ECR::Repository", 1);
    template.hasResourceProperties("AWS::ECR::Repository", {
      RepositoryName: "aisuite-dev-rag-batch",
    });
  });

  it("creates no always-on service or schedule", () => {
    const { template } = synthesize();

    template.resourceCountIs("AWS::ECS::Service", 0);
    template.resourceCountIs("AWS::Events::Rule", 0);
    template.resourceCountIs("AWS::Scheduler::Schedule", 0);
  });

  it("exposes private RunTask network settings using the app security group", () => {
    const { batch } = synthesize();

    expect(batch.runTaskNetworkConfiguration.assignPublicIp).toBe(false);
    expect(batch.runTaskNetworkConfiguration.securityGroups).toEqual([
      batch.appSecurityGroup,
    ]);
    expect(batch.runTaskNetworkConfiguration.subnetSelection).toEqual({
      subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
    });
  });
});
