import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import type * as s3 from "aws-cdk-lib/aws-s3";
import type * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

import { APPLICATION_NAME, type DeploymentConfig } from "../deployment-config";

export const BATCH_IMAGE_TAG_CONTEXT_KEY = "aisuite:batchImageTag";

const DEFAULT_BATCH_IMAGE_TAG = "latest";

const BEDROCK_MODEL_ID = "us.amazon.nova-pro-v1:0";
const BEDROCK_EMBED_MODEL_ID = "us.cohere.embed-v4:0";

export interface BatchConstructProps {
  appSecurityGroup: ec2.ISecurityGroup;
  dbSecret: secretsmanager.ISecret;
  deploymentConfig: DeploymentConfig;
  documentsBucket: s3.IBucket;
  pipelineCodeBucket: s3.IBucket;
  pipelineTempBucket: s3.IBucket;
  postProcessingBucket: s3.IBucket;
  taskRole: iam.IRole;
  vpc: ec2.IVpc;
}

export interface BatchRunTaskNetworkConfiguration {
  assignPublicIp: false;
  securityGroups: ec2.ISecurityGroup[];
  subnetSelection: ec2.SubnetSelection;
}

export class BatchConstruct extends Construct {
  public readonly appSecurityGroup: ec2.ISecurityGroup;
  public readonly cluster: ecs.Cluster;
  public readonly executionRole: iam.Role;
  public readonly preProcessingTaskDefinition: ecs.FargateTaskDefinition;
  public readonly ragProcessTaskDefinition: ecs.FargateTaskDefinition;
  public readonly repository: ecr.Repository;
  public readonly runTaskNetworkConfiguration: BatchRunTaskNetworkConfiguration;
  public readonly vpc: ec2.IVpc;

  public constructor(
    scope: Construct,
    id: string,
    props: BatchConstructProps,
  ) {
    super(scope, id);

    const { name, protectedEnvironment } = props.deploymentConfig;
    const resourcePrefix = `${APPLICATION_NAME}-${name}`;

    this.appSecurityGroup = props.appSecurityGroup;
    this.vpc = props.vpc;
    this.runTaskNetworkConfiguration = {
      assignPublicIp: false,
      securityGroups: [props.appSecurityGroup],
      subnetSelection: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    };

    this.cluster = new ecs.Cluster(this, "Cluster", {
      clusterName: `${resourcePrefix}-rag-batch`,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
      vpc: props.vpc,
    });

    this.repository = new ecr.Repository(this, "Repository", {
      emptyOnDelete: !protectedEnvironment,
      imageScanOnPush: true,
      removalPolicy: protectedEnvironment
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      repositoryName: `${resourcePrefix}-rag-batch`,
    });

    this.executionRole = new iam.Role(this, "ExecutionRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: "Execution role for AISuite on-demand RAG batch tasks.",
    });
    this.repository.grantPull(this.executionRole);

    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/${resourcePrefix}-rag-batch`,
      removalPolicy: protectedEnvironment
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      retention: logs.RetentionDays.ONE_MONTH,
    });
    logGroup.grantWrite(this.executionRole);

    const environment = {
      AWS_REGION: cdk.Stack.of(this).region,
      BEDROCK_EMBED_MODEL_ID,
      BEDROCK_MODEL_ID,
      DB_SECRET_ARN: props.dbSecret.secretArn,
      DOCUMENTS_BUCKET: props.documentsBucket.bucketName,
      PIPELINE_CODE_BUCKET: props.pipelineCodeBucket.bucketName,
      PIPELINE_TEMP_BUCKET: props.pipelineTempBucket.bucketName,
      POST_PROCESSING_BUCKET: props.postProcessingBucket.bucketName,
    };
    const imageTag =
      (this.node.tryGetContext(BATCH_IMAGE_TAG_CONTEXT_KEY) as
        | string
        | undefined) ?? DEFAULT_BATCH_IMAGE_TAG;
    const image = ecs.ContainerImage.fromEcrRepository(
      this.repository,
      imageTag,
    );

    this.preProcessingTaskDefinition = this.createTaskDefinition({
      command: ["python", "-m", "data_preprocessing.pre_processing"],
      environment,
      family: `${resourcePrefix}-pre-processing`,
      id: "PreProcessing",
      image,
      logGroup,
      taskRole: props.taskRole,
    });
    this.ragProcessTaskDefinition = this.createTaskDefinition({
      command: ["python", "-m", "data_embeddings_storage.rag_process"],
      environment,
      family: `${resourcePrefix}-rag-process`,
      id: "RagProcess",
      image,
      logGroup,
      taskRole: props.taskRole,
    });
  }

  private createTaskDefinition(props: {
    command: string[];
    environment: Record<string, string>;
    family: string;
    id: string;
    image: ecs.ContainerImage;
    logGroup: logs.ILogGroup;
    taskRole: iam.IRole;
  }): ecs.FargateTaskDefinition {
    const taskDefinition = new ecs.FargateTaskDefinition(
      this,
      `${props.id}TaskDefinition`,
      {
        cpu: 1024,
        executionRole: this.executionRole,
        family: props.family,
        memoryLimitMiB: 2048,
        runtimePlatform: {
          cpuArchitecture: ecs.CpuArchitecture.X86_64,
          operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
        },
        taskRole: props.taskRole,
      },
    );

    taskDefinition.addContainer(`${props.id}Container`, {
      command: props.command,
      environment: props.environment,
      image: props.image,
      logging: ecs.LogDrivers.awsLogs({
        logGroup: props.logGroup,
        streamPrefix: props.family,
      }),
    });

    return taskDefinition;
  }
}
