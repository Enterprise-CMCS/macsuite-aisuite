import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import type * as s3 from "aws-cdk-lib/aws-s3";
import type * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

import {
  APPLICATION_NAME,
  type DeploymentConfig,
  type DeploymentEnvironmentName,
} from "../deployment-config";

export const API_CONTAINER_PORT = 8001;

export const API_HEALTH_CHECK_PATH = "/health";

export const API_ACTIVATION_CONTEXT_KEY = "aisuite:activateApi";

export const API_IMAGE_TAG_CONTEXT_KEY = "aisuite:apiImageTag";

const DEFAULT_API_IMAGE_TAG = "latest";

const BEDROCK_MODEL_ID = "us.amazon.nova-pro-v1:0";
const BEDROCK_EMBED_MODEL_ID = "us.cohere.embed-v4:0";

const TASK_SIZE_BY_ENVIRONMENT = {
  dev: { cpu: 256, memoryLimitMiB: 512 },
  qa: { cpu: 256, memoryLimitMiB: 512 },
  uat: { cpu: 512, memoryLimitMiB: 1024 },
  prod: { cpu: 512, memoryLimitMiB: 1024 },
} as const satisfies Record<
  DeploymentEnvironmentName,
  { cpu: number; memoryLimitMiB: number }
>;

export interface ComputeConstructProps {
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

export class ComputeConstruct extends Construct {
  public readonly cluster: ecs.Cluster;
  public readonly loadBalancer: elbv2.ApplicationLoadBalancer;
  public readonly repository: ecr.Repository;
  public readonly service: ecs.FargateService;

  public constructor(
    scope: Construct,
    id: string,
    props: ComputeConstructProps,
  ) {
    super(scope, id);

    const { name, protectedEnvironment } = props.deploymentConfig;
    const { region } = cdk.Stack.of(this);
    const serviceName = `${APPLICATION_NAME}-${name}-rag-api`;
    const removalPolicy = protectedEnvironment
      ? cdk.RemovalPolicy.RETAIN
      : cdk.RemovalPolicy.DESTROY;

    this.cluster = new ecs.Cluster(this, "Cluster", {
      clusterName: serviceName,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
      vpc: props.vpc,
    });

    this.repository = new ecr.Repository(this, "Repository", {
      emptyOnDelete: !protectedEnvironment,
      imageScanOnPush: true,
      lifecycleRules: [{ maxImageCount: 10 }],
      removalPolicy,
      repositoryName: serviceName,
    });

    const executionRole = new iam.Role(this, "ExecutionRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: "Execution role for the AISuite RAG API service.",
    });
    this.repository.grantPull(executionRole);

    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/${serviceName}`,
      removalPolicy,
      retention: logs.RetentionDays.ONE_MONTH,
    });
    logGroup.grantWrite(executionRole);

    const taskDefinition = new ecs.FargateTaskDefinition(
      this,
      "TaskDefinition",
      {
        ...TASK_SIZE_BY_ENVIRONMENT[name],
        executionRole,
        family: serviceName,
        runtimePlatform: {
          cpuArchitecture: ecs.CpuArchitecture.X86_64,
          operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
        },
        taskRole: props.taskRole,
      },
    );

    const imageTag =
      (this.node.tryGetContext(API_IMAGE_TAG_CONTEXT_KEY) as
        | string
        | undefined) ?? DEFAULT_API_IMAGE_TAG;
    const activationContext = this.node.tryGetContext(
      API_ACTIVATION_CONTEXT_KEY,
    ) as unknown;
    const apiActivated =
      activationContext === true || activationContext === "true";

    const container = taskDefinition.addContainer("Container", {
      containerName: "rag-api",
      environment: {
        AWS_DEFAULT_REGION: region,
        AWS_REGION: region,
        BEDROCK_EMBED_MODEL_ID,
        BEDROCK_MODEL_ID,
        DB_SECRET_ARN: props.dbSecret.secretArn,
        DOCUMENTS_BUCKET: props.documentsBucket.bucketName,
        PIPELINE_CODE_BUCKET: props.pipelineCodeBucket.bucketName,
        PIPELINE_TEMP_BUCKET: props.pipelineTempBucket.bucketName,
        POST_PROCESSING_BUCKET: props.postProcessingBucket.bucketName,
      },
      image: ecs.ContainerImage.fromEcrRepository(this.repository, imageTag),
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "rag-api" }),
    });
    container.addPortMappings({
      containerPort: API_CONTAINER_PORT,
      protocol: ecs.Protocol.TCP,
    });

    this.service = new ecs.FargateService(this, "Service", {
      circuitBreaker: { enable: true, rollback: false },
      cluster: this.cluster,
      desiredCount: apiActivated ? (protectedEnvironment ? 2 : 1) : 0,
      healthCheckGracePeriod: cdk.Duration.minutes(5),
      maxHealthyPercent: 200,
      minHealthyPercent: 0,
      securityGroups: [props.appSecurityGroup],
      serviceName,
      taskDefinition,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    const loadBalancerSecurityGroup = new ec2.SecurityGroup(
      this,
      "AlbSecurityGroup",
      {
        allowAllOutbound: false,
        description: "AISuite RAG API internal ALB; ingress from the VPC only.",
        vpc: props.vpc,
      },
    );

    this.loadBalancer = new elbv2.ApplicationLoadBalancer(this, "Alb", {
      internetFacing: false,
      loadBalancerName: serviceName,
      securityGroup: loadBalancerSecurityGroup,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    const listener = this.loadBalancer.addListener("Listener", {
      open: false,
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
    });

    listener.connections.allowFrom(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(80),
      "RAG API HTTP from inside the VPC",
    );

    listener.addTargets("Targets", {
      deregistrationDelay: cdk.Duration.seconds(30),
      healthCheck: {
        healthyThresholdCount: 2,
        interval: cdk.Duration.seconds(30),
        path: API_HEALTH_CHECK_PATH,
        timeout: cdk.Duration.seconds(5),
        unhealthyThresholdCount: 3,
      },
      port: API_CONTAINER_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [
        this.service.loadBalancerTarget({
          containerName: container.containerName,
          containerPort: API_CONTAINER_PORT,
        }),
      ],
    });
  }
}
