import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as logs from "aws-cdk-lib/aws-logs";
import * as rds from "aws-cdk-lib/aws-rds";
import type * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

import type {
  DeploymentConfig,
  DeploymentEnvironmentName,
} from "../deployment-config";
import { POSTGRES_PORT } from "./networking-construct";

export const DATABASE_NAME = "vectordb";

const INSTANCE_SIZE_BY_ENVIRONMENT = {
  dev: ec2.InstanceSize.MEDIUM,
  qa: ec2.InstanceSize.MEDIUM,
  uat: ec2.InstanceSize.LARGE,
  prod: ec2.InstanceSize.LARGE,
} as const satisfies Record<DeploymentEnvironmentName, ec2.InstanceSize>;

export interface DatabaseConstructProps {
  appDbSecret: rds.DatabaseSecret;
  appSecurityGroup: ec2.ISecurityGroup;
  dbSecret: secretsmanager.ISecret;
  dbSecurityGroup: ec2.SecurityGroup;
  deploymentConfig: DeploymentConfig;
  vpc: ec2.IVpc;
}

export class DatabaseConstruct extends Construct {
  public readonly appDbSecret: secretsmanager.ISecret;
  public readonly endpointAddress: string;
  public readonly instance: rds.DatabaseInstance;
  public readonly secret: secretsmanager.ISecret;

  public constructor(
    scope: Construct,
    id: string,
    props: DatabaseConstructProps,
  ) {
    super(scope, id);

    const { name, protectedEnvironment, vpnSecurityGroupId } =
      props.deploymentConfig;

    const securityGroups: ec2.ISecurityGroup[] = [props.dbSecurityGroup];
    if (vpnSecurityGroupId) {
      securityGroups.push(
        ec2.SecurityGroup.fromSecurityGroupId(
          this,
          "VpnAccessSecurityGroup",
          vpnSecurityGroupId,
          { mutable: false },
        ),
      );
    }

    this.instance = new rds.DatabaseInstance(this, "RagDatabase", {
      allocatedStorage: 100,
      backupRetention: cdk.Duration.days(protectedEnvironment ? 14 : 7),
      cloudwatchLogsExports: ["postgresql"],
      cloudwatchLogsRetention: logs.RetentionDays.ONE_MONTH,
      credentials: rds.Credentials.fromSecret(props.dbSecret),
      databaseName: DATABASE_NAME,
      deletionProtection: protectedEnvironment,
      engine: rds.DatabaseInstanceEngine.postgres({
        version: rds.PostgresEngineVersion.VER_16,
      }),
      instanceType: ec2.InstanceType.of(
        ec2.InstanceClass.T4G,
        INSTANCE_SIZE_BY_ENVIRONMENT[name],
      ),
      maxAllocatedStorage: 500,
      multiAz: name === "prod",
      port: POSTGRES_PORT,
      publiclyAccessible: false,
      removalPolicy: protectedEnvironment
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      securityGroups,
      storageEncrypted: true,
      storageType: rds.StorageType.GP3,
      vpc: props.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });

    props.dbSecurityGroup.addIngressRule(
      props.appSecurityGroup,
      ec2.Port.tcp(POSTGRES_PORT),
      "PostgreSQL from the AISuite application tier",
    );

    this.secret = this.instance.secret ?? props.dbSecret;
    this.endpointAddress = this.instance.dbInstanceEndpointAddress;

    this.appDbSecret = props.appDbSecret.attach(this.instance);
    this.instance.addRotationMultiUser("AppDbUser", {
      automaticallyAfter: cdk.Duration.days(30),
      secret: this.appDbSecret,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    });
  }
}
