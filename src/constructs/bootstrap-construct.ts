import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import type * as ecr from "aws-cdk-lib/aws-ecr";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import type * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

import { APPLICATION_NAME, type DeploymentConfig } from "../deployment-config";
import { BATCH_IMAGE_TAG_CONTEXT_KEY } from "./batch-construct";

const DEFAULT_BATCH_IMAGE_TAG = "latest";
const MINIMIZE_IAM_POLICIES_CONTEXT_KEY = "@aws-cdk/aws-iam:minimizePolicies";

export interface BootstrapConstructProps {
  appDbSecret: secretsmanager.ISecret;
  appSecurityGroup: ec2.ISecurityGroup;
  cluster: ecs.ICluster;
  deploymentConfig: DeploymentConfig;
  masterDbSecret: secretsmanager.ISecret;
  repository: ecr.IRepository;
  vpc: ec2.IVpc;
}

export class BootstrapConstruct extends Construct {
  public readonly cluster: ecs.ICluster;
  public readonly securityGroup: ec2.ISecurityGroup;
  public readonly subnetIds: string[];
  public readonly taskDefinition: ecs.FargateTaskDefinition;

  public constructor(
    scope: Construct,
    id: string,
    props: BootstrapConstructProps,
  ) {
    super(scope, id);

    // ECS adds one grant per injected secret; policy minimization combines
    // those grants into one statement scoped to the two secret ARNs.
    this.node.setContext(MINIMIZE_IAM_POLICIES_CONTEXT_KEY, true);

    const { name, protectedEnvironment } = props.deploymentConfig;
    const family = `${APPLICATION_NAME}-${name}-rag-db-bootstrap`;
    const executionRole = new iam.Role(this, "ExecutionRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: "Execution role for the AISuite database bootstrap task.",
    });

    props.repository.grantPull(executionRole);

    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/${family}`,
      removalPolicy: protectedEnvironment
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      retention: logs.RetentionDays.ONE_MONTH,
    });
    logGroup.grantWrite(executionRole);

    this.taskDefinition = new ecs.FargateTaskDefinition(
      this,
      "TaskDefinition",
      {
        cpu: 256,
        executionRole,
        family,
        memoryLimitMiB: 512,
        runtimePlatform: {
          cpuArchitecture: ecs.CpuArchitecture.X86_64,
          operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
        },
      },
    );

    const imageTag =
      (this.node.tryGetContext(BATCH_IMAGE_TAG_CONTEXT_KEY) as
        | string
        | undefined) ?? DEFAULT_BATCH_IMAGE_TAG;

    this.taskDefinition.addContainer("Container", {
      command: [
        "python",
        "-m",
        "data_embeddings_storage.database.bootstrap",
      ],
      image: ecs.ContainerImage.fromEcrRepository(props.repository, imageTag),
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: "bootstrap",
      }),
      secrets: {
        APP_PASSWORD: ecs.Secret.fromSecretsManager(
          props.appDbSecret,
          "password",
        ),
        PGDATABASE: ecs.Secret.fromSecretsManager(
          props.masterDbSecret,
          "dbname",
        ),
        PGHOST: ecs.Secret.fromSecretsManager(props.masterDbSecret, "host"),
        PGPASSWORD: ecs.Secret.fromSecretsManager(
          props.masterDbSecret,
          "password",
        ),
        PGPORT: ecs.Secret.fromSecretsManager(props.masterDbSecret, "port"),
        PGUSER: ecs.Secret.fromSecretsManager(
          props.masterDbSecret,
          "username",
        ),
      },
    });

    this.cluster = props.cluster;
    this.securityGroup = props.appSecurityGroup;
    this.subnetIds = props.vpc.selectSubnets({
      subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
    }).subnetIds;
  }
}
