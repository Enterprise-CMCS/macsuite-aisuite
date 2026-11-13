import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr_assets from "aws-cdk-lib/aws-ecr-assets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3n from "aws-cdk-lib/aws-s3-notifications";
import type * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

import { APPLICATION_NAME, type DeploymentConfig } from "../deployment-config";
import {
  BEDROCK_EMBED_MODEL_ID,
  COMMON_EMBEDDINGS_TABLE_NAME,
  COMMON_INPUT_PREFIX,
  INGEST_MAX_CONCURRENCY,
  INGEST_TIMEOUT,
  RAG_SERVICE_DIR,
} from "./pre-processing-lambda-construct";

export const RAG_OUTPUT_PREFIX = "RAG_Output/";
export const COMMON_RAG_OUTPUT_PREFIX = `${RAG_OUTPUT_PREFIX}${COMMON_INPUT_PREFIX}`;

export interface EmbeddingsLambdaConstructProps {
  appSecurityGroup: ec2.ISecurityGroup;
  dbSecret: secretsmanager.ISecret;
  deploymentConfig: DeploymentConfig;
  postProcessingBucket: s3.IBucket;
  vpc: ec2.IVpc;
}

export class EmbeddingsLambdaConstruct extends Construct {
  public readonly function: lambda.DockerImageFunction;

  public constructor(
    scope: Construct,
    id: string,
    props: EmbeddingsLambdaConstructProps,
  ) {
    super(scope, id);

    const { name, protectedEnvironment } = props.deploymentConfig;
    const { account } = cdk.Stack.of(this);
    const functionName = `${APPLICATION_NAME}-${name}-embeddings`;
    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/aws/lambda/${functionName}`,
      removalPolicy: protectedEnvironment
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      retention: logs.RetentionDays.ONE_MONTH,
    });

    this.function = new lambda.DockerImageFunction(this, "Function", {
      architecture: lambda.Architecture.ARM_64,
      code: lambda.DockerImageCode.fromImageAsset(RAG_SERVICE_DIR, {
        cmd: ["data_embeddings_storage.rag_process.handler"],
        file: "Dockerfile.lambda",
        platform: ecr_assets.Platform.LINUX_ARM64,
      }),
      description:
        "Allocates, fills and publishes the versioned per-state embeddings table in RDS PostgreSQL; invoked by the manifest workflow.",
      environment: {
        BEDROCK_EMBED_MODEL_ID,
        COMMON_EMBEDDINGS_TABLE_NAME,
        COMMON_INPUT_PREFIX,
        DB_SECRET_ARN: props.dbSecret.secretArn,
        POST_PROCESSING_BUCKET: props.postProcessingBucket.bucketName,
      },
      functionName,
      logGroup,
      memorySize: 2048,
      reservedConcurrentExecutions: INGEST_MAX_CONCURRENCY,
      securityGroups: [props.appSecurityGroup],
      timeout: INGEST_TIMEOUT,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    props.postProcessingBucket.grantRead(this.function);
    props.dbSecret.grantRead(this.function);

    this.function.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel"],
        resources: [
          "arn:aws:bedrock:*::foundation-model/cohere.embed-v4:0",
          `arn:aws:bedrock:*:${account}:inference-profile/${BEDROCK_EMBED_MODEL_ID}`,
          `arn:aws:bedrock:*::inference-profile/${BEDROCK_EMBED_MODEL_ID}`,
        ],
        sid: "BedrockEmbeddingInvocation",
      }),
    );

    props.postProcessingBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.LambdaDestination(this.function),
      { prefix: COMMON_RAG_OUTPUT_PREFIX, suffix: ".json" },
    );
  }
}
