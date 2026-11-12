import * as path from "node:path";

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr_assets from "aws-cdk-lib/aws-ecr-assets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3n from "aws-cdk-lib/aws-s3-notifications";
import { Construct } from "constructs";

import { APPLICATION_NAME, type DeploymentConfig } from "../deployment-config";

export const BEDROCK_MODEL_ID = "us.amazon.nova-pro-v1:0";
export const BEDROCK_EMBED_MODEL_ID = "us.cohere.embed-v4:0";
export const COMMON_INPUT_PREFIX = "common/";
export const COMMON_EMBEDDINGS_TABLE_NAME = "embeddings_cms_regs";
export const DOCUMENT_SUFFIXES = [".pdf", ".PDF", ".docx", ".DOCX"] as const;
export const RAG_SERVICE_DIR = path.resolve(process.cwd(), "services", "rag");

// Standard Lambda maximum.
export const INGEST_TIMEOUT = cdk.Duration.minutes(15);
// Shared by the Lambdas' reserved concurrency and the workflow's Map concurrency,
// protecting BDA quotas and database connections.
export const INGEST_MAX_CONCURRENCY = 5;

export interface PreProcessingLambdaConstructProps {
  appSecurityGroup: ec2.ISecurityGroup;
  deploymentConfig: DeploymentConfig;
  documentsBucket: s3.IBucket;
  pipelineTempBucket: s3.IBucket;
  postProcessingBucket: s3.IBucket;
  vpc: ec2.IVpc;
}

export class PreProcessingLambdaConstruct extends Construct {
  public readonly function: lambda.DockerImageFunction;

  public constructor(
    scope: Construct,
    id: string,
    props: PreProcessingLambdaConstructProps,
  ) {
    super(scope, id);

    const { name, protectedEnvironment } = props.deploymentConfig;
    const { account } = cdk.Stack.of(this);
    const functionName = `${APPLICATION_NAME}-${name}-pre-processing`;
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
        cmd: ["data_preprocessing.pre_processing.handler"],
        file: "Dockerfile.lambda",
        platform: ecr_assets.Platform.LINUX_ARM64,
      }),
      description:
        "Pre-processes one manifest document with Bedrock Data Automation and writes its RAG_Output JSON; invoked by the manifest workflow.",
      environment: {
        BEDROCK_EMBED_MODEL_ID,
        BEDROCK_MODEL_ID,
        DOCUMENTS_BUCKET: props.documentsBucket.bucketName,
        PIPELINE_TEMP_BUCKET: props.pipelineTempBucket.bucketName,
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

    props.documentsBucket.grantRead(this.function);
    props.postProcessingBucket.grantReadWrite(this.function);
    props.postProcessingBucket.grantDelete(this.function);
    props.pipelineTempBucket.grantReadWrite(this.function);

    this.function.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel"],
        resources: [
          "arn:aws:bedrock:*::foundation-model/amazon.nova-pro-v1:0",
          "arn:aws:bedrock:*::foundation-model/cohere.embed-v4:0",
          `arn:aws:bedrock:*:${account}:inference-profile/${BEDROCK_MODEL_ID}`,
          `arn:aws:bedrock:*:${account}:inference-profile/${BEDROCK_EMBED_MODEL_ID}`,
          `arn:aws:bedrock:*::inference-profile/${BEDROCK_MODEL_ID}`,
          `arn:aws:bedrock:*::inference-profile/${BEDROCK_EMBED_MODEL_ID}`,
        ],
        sid: "BedrockModelInvocation",
      }),
    );
    this.function.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:CreateDataAutomationProject",
          "bedrock:GetDataAutomationProject",
          "bedrock:GetDataAutomationStatus",
          "bedrock:InvokeDataAutomationAsync",
          "bedrock:UpdateDataAutomationProject",
        ],
        resources: [
          `arn:aws:bedrock:*:${account}:data-automation-project/*`,
          `arn:aws:bedrock:*:${account}:data-automation-profile/*`,
          `arn:aws:bedrock:*::data-automation-profile/*`,
          `arn:aws:bedrock:*:${account}:data-automation-invocation/*`,
        ],
        sid: "BedrockDataAutomation",
      }),
    );
    this.function.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:ListDataAutomationProjects"],
        resources: ["*"],
        sid: "BedrockDataAutomationList",
      }),
    );

    const destination = new s3n.LambdaDestination(this.function);
    for (const suffix of DOCUMENT_SUFFIXES) {
      props.documentsBucket.addEventNotification(
        s3.EventType.OBJECT_CREATED,
        destination,
        { prefix: COMMON_INPUT_PREFIX, suffix },
      );
    }
  }
}
