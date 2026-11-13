import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr_assets from "aws-cdk-lib/aws-ecr-assets";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
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
// Superseded embeddings_<state>_v<N> tables are dropped this many days after a newer
// READY version replaced them. The schedule is monthly, so a table is actually dropped
// 30-61 days after it was superseded.
export const EMBEDDINGS_RETENTION_DAYS = 30;

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

    // Both triggers (S3 notifications and the expiry schedule) invoke asynchronously, so
    // Lambda, not the caller, retries a function error. These are Lambda's defaults,
    // pinned so the expiry run's "fails, then is retried" behaviour is explicit.
    this.function.configureAsyncInvoke({
      maxEventAge: cdk.Duration.hours(6),
      retryAttempts: 2,
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

    // 06:00 UTC on the 1st of every month: drop expired embeddings versions.
    new events.Rule(this, "ExpireVersionsSchedule", {
      description: "Drop superseded embeddings versions past their retention period",
      schedule: events.Schedule.cron({
        day: "1",
        hour: "6",
        minute: "0",
        month: "*",
        year: "*",
      }),
      targets: [
        new targets.LambdaFunction(this.function, {
          event: events.RuleTargetInput.fromObject({
            action: "expire",
            retentionDays: EMBEDDINGS_RETENTION_DAYS,
          }),
          // Covers failed deliveries (e.g. throttling) only; function errors are
          // retried by the async invoke config above.
          retryAttempts: 2,
        }),
      ],
    });
  }
}
