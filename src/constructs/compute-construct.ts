import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import type * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

import {
  APPLICATION_NAME,
  type DeploymentConfig,
  type DeploymentEnvironmentName,
} from "../deployment-config";
import { STUB_VPC_CONTEXT_KEY } from "./networking-construct";

export const API_CONTAINER_PORT = 8001;

/** Placeholder ACM ARN used only when synthesizing with `aisuite:stubVpc`. */
const STUB_ALB_CERTIFICATE_ARN =
  "arn:aws:acm:us-east-1:000000000000:certificate/00000000-0000-0000-0000-000000000000";

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

    // Allow HTTPS from the CMS Cloud VPN Prefix List (custom source)
    loadBalancerSecurityGroup.addIngressRule(
      ec2.Peer.prefixList("pl-006315be223e9c9a7"),
      ec2.Port.tcp(443),
      "Allow Traffic from CMS Cloud VPN Prefix List",
    );

    this.loadBalancer = new elbv2.ApplicationLoadBalancer(this, "Alb", {
      internetFacing: false,
      loadBalancerName: serviceName,
      securityGroup: loadBalancerSecurityGroup,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      deletionProtection: protectedEnvironment,
    });

    // Configure ALB access logging to the central access-logs bucket under
    // the `ALB-Access-Logs` prefix. We reference the existing bucket by name.
    try {
      const accessLogsBucket = s3.Bucket.fromBucketName(
        this,
        "CentralAccessLogsBucket",
        props.deploymentConfig.accessLogsBucketName,
      );
      // Use a prefix based on the ALB/service name so logs are organized per ALB.
      // Prefix must not start or end with '/'. Use `ALB-Access-Logs/<serviceName>`.
      this.loadBalancer.logAccessLogs(accessLogsBucket, `ALB-Access-Logs/${serviceName}`);
    } catch {
      // Best-effort: if the method or bucket doesn't resolve during synth, continue.
    }

    // Require an ACM certificate ARN for HTTPS (443). When synthesizing with
    // stub VPC (unit tests / PR CI), fall back to a placeholder ARN so qa/uat/prod
    // templates can still synth before real certs are configured.
    const configuredCertArn = props.deploymentConfig.albCertificateArn;
    const stubVpcContext = this.node.tryGetContext(STUB_VPC_CONTEXT_KEY);
    const usingStubVpc = stubVpcContext === true || stubVpcContext === "true";
    const albCertArn = configuredCertArn ?? (usingStubVpc ? STUB_ALB_CERTIFICATE_ARN : undefined);
    if (!albCertArn) {
      throw new Error(
        "ALB certificate ARN is required in deploymentConfig.albCertificateArn to create an HTTPS listener",
      );
    }
    const cert = acm.Certificate.fromCertificateArn(this, "AlbCertificate", albCertArn);
    const listener = this.loadBalancer.addListener("Listener", {
      open: false,
      port: 443,
      protocol: elbv2.ApplicationProtocol.HTTPS,
      certificates: [cert],
    });

    listener.connections.allowFrom(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(443),
      "RAG API HTTPS (443) from inside the VPC",
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
