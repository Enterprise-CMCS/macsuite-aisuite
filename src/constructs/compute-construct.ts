import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

import {
  APPLICATION_NAME,
  type DeploymentConfig,
  type DeploymentEnvironmentName,
} from "../deployment-config";
import { STUB_VPC_CONTEXT_KEY } from "./networking-construct";
import {
  BEDROCK_EMBED_MODEL_ID,
  BEDROCK_MODEL_ID,
} from "./pre-processing-lambda-construct";

export const API_CONTAINER_PORT = 8001;

/** Placeholder ACM ARN used only when synthesizing with `aisuite:stubVpc`. */
const STUB_ALB_CERTIFICATE_ARN =
  "arn:aws:acm:us-east-1:000000000000:certificate/00000000-0000-0000-0000-000000000000";

export const API_HEALTH_CHECK_PATH = "/health";

export const API_ACTIVATION_CONTEXT_KEY = "aisuite:activateApi";

export const API_IMAGE_TAG_CONTEXT_KEY = "aisuite:apiImageTag";

const DEFAULT_API_IMAGE_TAG = "latest";

// The requirement review task runs `python -m search.manifest_review` from the API image.
export const REVIEW_CONTAINER_NAME = "requirement-review";
// Concurrent requirement reviews per task; bounded by Bedrock throughput, not task size.
const REVIEW_CONCURRENCY = 4;
const REVIEW_TASK_SIZE = { cpu: 1024, memoryLimitMiB: 2048 } as const;

const RUNTIME_PLATFORM: ecs.RuntimePlatform = {
  cpuArchitecture: ecs.CpuArchitecture.X86_64,
  operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
};

const TASK_SIZE_BY_ENVIRONMENT = {
  dev: { cpu: 256, memoryLimitMiB: 512 },
  qa: { cpu: 256, memoryLimitMiB: 512 },
  uat: { cpu: 512, memoryLimitMiB: 1024 },
  prod: { cpu: 512, memoryLimitMiB: 1024 },
} as const satisfies Record<
  DeploymentEnvironmentName,
  { cpu: number; memoryLimitMiB: number }
>;

/** Minimum task size when the Falcon sensor shares the task with the API. */
const FALCON_MIN_TASK_SIZE = { cpu: 512, memoryLimitMiB: 1024 } as const;

const FALCON_VOLUME_NAME = "crowdstrike-falcon-volume";
const FALCON_MOUNT_PATH = "/tmp/CrowdStrike";
const FALCON_ROOTFS = `${FALCON_MOUNT_PATH}/rootfs`;
const FALCON_LOADER = [
  `${FALCON_ROOTFS}/lib64/ld-linux-x86-64.so.2`,
  "--library-path",
  `${FALCON_ROOTFS}/lib64`,
  `${FALCON_ROOTFS}/bin/bash`,
] as const;

export interface ComputeConstructProps {
  appSecurityGroup: ec2.ISecurityGroup;
  dbSecret: secretsmanager.ISecret;
  deploymentConfig: DeploymentConfig;
  documentsBucket: s3.IBucket;
  pipelineTempBucket: s3.IBucket;
  taskRole: iam.IRole;
  vpc: ec2.IVpc;
}

export class ComputeConstruct extends Construct {
  public readonly apiContainer: ecs.ContainerDefinition;
  public readonly cluster: ecs.Cluster;
  public readonly listener: elbv2.ApplicationListener;
  public readonly loadBalancer: elbv2.ApplicationLoadBalancer;
  public readonly loadBalancerSecurityGroup: ec2.ISecurityGroup;
  public readonly repository: ecr.Repository;
  public readonly reviewContainer: ecs.ContainerDefinition;
  public readonly reviewTaskDefinition: ecs.FargateTaskDefinition;
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
      description:
        "Execution role for the AISuite RAG API service and requirement review task.",
    });
    this.repository.grantPull(executionRole);

    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/${serviceName}`,
      removalPolicy,
      retention: logs.RetentionDays.ONE_MONTH,
    });
    logGroup.grantWrite(executionRole);

    const falconConfig = props.deploymentConfig.falcon;
    const baseTaskSize = TASK_SIZE_BY_ENVIRONMENT[name];
    const taskSize = falconConfig
      ? {
          cpu: Math.max(baseTaskSize.cpu, FALCON_MIN_TASK_SIZE.cpu),
          memoryLimitMiB: Math.max(
            baseTaskSize.memoryLimitMiB,
            FALCON_MIN_TASK_SIZE.memoryLimitMiB,
          ),
        }
      : baseTaskSize;

    const taskDefinition = new ecs.FargateTaskDefinition(
      this,
      "TaskDefinition",
      {
        ...taskSize,
        executionRole,
        family: serviceName,
        runtimePlatform: RUNTIME_PLATFORM,
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

    const image = ecs.ContainerImage.fromEcrRepository(this.repository, imageTag);
    // Both containers read the database, Bedrock and the manifest run files in the temp bucket.
    const sharedEnvironment = {
      AWS_DEFAULT_REGION: region,
      AWS_REGION: region,
      BEDROCK_EMBED_MODEL_ID,
      BEDROCK_MODEL_ID,
      DB_SECRET_ARN: props.dbSecret.secretArn,
      PIPELINE_TEMP_BUCKET: props.pipelineTempBucket.bucketName,
    };

    let falconInit: ecs.ContainerDefinition | undefined;
    let falconContainerOptions:
      | Pick<
          ecs.ContainerDefinitionOptions,
          "entryPoint" | "linuxParameters" | "secrets"
        >
      | undefined;

    if (falconConfig) {
      taskDefinition.addVolume({ name: FALCON_VOLUME_NAME });

      const registryCredentials = falconConfig.registryCredentialsSecretName
        ? secretsmanager.Secret.fromSecretNameV2(
            this,
            "FalconRegistryCredentials",
            falconConfig.registryCredentialsSecretName,
          )
        : undefined;

      falconInit = taskDefinition.addContainer(
        "crowdstrike-falcon-init-container",
        {
          image: ecs.ContainerImage.fromRegistry(
            falconConfig.image,
            registryCredentials ? { credentials: registryCredentials } : {},
          ),
          essential: false,
          user: "0:0",
          readonlyRootFilesystem: true,
          logging: ecs.LogDrivers.awsLogs({
            logGroup,
            streamPrefix: "falcon-init",
          }),
          entryPoint: [
            "/bin/bash",
            "-c",
            `chmod u+rwx ${FALCON_MOUNT_PATH} && mkdir -p ${FALCON_ROOTFS}/lib && cp -r /bin /etc /lib64 /usr /entrypoint-ecs.sh ${FALCON_ROOTFS} && chmod -R a=rX ${FALCON_MOUNT_PATH}`,
          ],
        },
      );
      falconInit.addMountPoints({
        containerPath: FALCON_MOUNT_PATH,
        readOnly: false,
        sourceVolume: FALCON_VOLUME_NAME,
      });

      const falconLinuxParameters = new ecs.LinuxParameters(
        this,
        "FalconLinuxParameters",
      );
      falconLinuxParameters.addCapabilities(ecs.Capability.SYS_PTRACE);
      const falconSecret = secretsmanager.Secret.fromSecretNameV2(
        this,
        "FalconSecret",
        falconConfig.secretName,
      );

      // Secrets arrive as env vars at runtime, so FALCONCTL_OPTS is assembled in
      // the container rather than written into the CloudFormation template.
      const falconBootstrap = [
        `export FALCONCTL_OPTS="--provisioning-token=\${FALCON_PROVISIONING_TOKEN} --tags=rag-api,${name},rag-api-${name} --cid=\${FALCON_CID}"`,
        `exec ${FALCON_LOADER.join(" ")} ${FALCON_ROOTFS}/entrypoint-ecs.sh "$@"`,
      ].join("; ");

      falconContainerOptions = {
        entryPoint: [...FALCON_LOADER, "-c", falconBootstrap, "--"],
        linuxParameters: falconLinuxParameters,
        secrets: {
          FALCON_CID: ecs.Secret.fromSecretsManager(falconSecret, "cid"),
          FALCON_PROVISIONING_TOKEN: ecs.Secret.fromSecretsManager(
            falconSecret,
            "provisioningToken",
          ),
        },
      };
    }

    this.apiContainer = taskDefinition.addContainer("Container", {
      containerName: "rag-api",
      environment: {
        ...sharedEnvironment,
        // The manifest API checks that every submitted document exists here.
        DOCUMENTS_BUCKET: props.documentsBucket.bucketName,
      },
      image,
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "rag-api" }),
      ...falconContainerOptions,
    });

    if (falconInit) {
      this.apiContainer.addMountPoints({
        containerPath: FALCON_MOUNT_PATH,
        readOnly: true,
        sourceVolume: FALCON_VOLUME_NAME,
      });
      this.apiContainer.addContainerDependencies({
        container: falconInit,
        condition: ecs.ContainerDependencyCondition.SUCCESS,
      });
    }

    this.apiContainer.addPortMappings({
      containerPort: API_CONTAINER_PORT,
      protocol: ecs.Protocol.TCP,
    });

    // Started per manifest run by the ingest workflow, never as a service.
    this.reviewTaskDefinition = new ecs.FargateTaskDefinition(
      this,
      "ReviewTaskDefinition",
      {
        ...REVIEW_TASK_SIZE,
        executionRole,
        family: `${APPLICATION_NAME}-${name}-requirement-review`,
        runtimePlatform: RUNTIME_PLATFORM,
        taskRole: props.taskRole,
      },
    );
    this.reviewContainer = this.reviewTaskDefinition.addContainer(
      "ReviewContainer",
      {
        command: ["python", "-m", "search.manifest_review"],
        containerName: REVIEW_CONTAINER_NAME,
        environment: {
          ...sharedEnvironment,
          REVIEW_CONCURRENCY: String(REVIEW_CONCURRENCY),
        },
        image,
        logging: ecs.LogDrivers.awsLogs({
          logGroup,
          streamPrefix: REVIEW_CONTAINER_NAME,
        }),
      },
    );

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

    this.loadBalancerSecurityGroup = loadBalancerSecurityGroup;

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
    this.listener = listener;

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
          containerName: this.apiContainer.containerName,
          containerPort: API_CONTAINER_PORT,
        }),
      ],
    });
  }
}
