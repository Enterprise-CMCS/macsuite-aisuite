import * as cdk from "aws-cdk-lib";
import type * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";

import { AppIamConstruct } from "./constructs/app-iam-construct";
import { BatchConstruct } from "./constructs/batch-construct";
import { BootstrapConstruct } from "./constructs/bootstrap-construct";
import { ComputeConstruct } from "./constructs/compute-construct";
import { DatabaseConstruct } from "./constructs/database-construct";
import { NetworkingConstruct } from "./constructs/networking-construct";
import { SecretsConstruct } from "./constructs/secrets-construct";
import { StorageConstruct } from "./constructs/storage-construct";
import type { DeploymentConfig } from "./deployment-config";

export interface AisuiteInfrastructureStackProps extends cdk.StackProps {
  deploymentConfig: DeploymentConfig;
  vpc?: ec2.IVpc;
}

export class AisuiteInfrastructureStack extends cdk.Stack {
  public constructor(
    scope: Construct,
    id: string,
    props: AisuiteInfrastructureStackProps,
  ) {
    super(scope, id, props);

    const { deploymentConfig } = props;

    const networking = new NetworkingConstruct(this, "Networking", {
      vpc: props.vpc,
      vpcName: deploymentConfig.vpcName,
    });

    const storage = new StorageConstruct(this, "Storage", {
      deploymentConfig,
    });

    const secrets = new SecretsConstruct(this, "Secrets", {
      deploymentConfig,
    });

    const database = new DatabaseConstruct(this, "Database", {
      appDbSecret: secrets.appDbSecret,
      appSecurityGroup: networking.appSecurityGroup,
      dbSecret: secrets.dbSecret,
      dbSecurityGroup: networking.dbSecurityGroup,
      deploymentConfig,
      vpc: networking.vpc,
    });

    const appIam = new AppIamConstruct(this, "AppIam", {
      dbSecret: database.appDbSecret,
      documentsBucket: storage.documentsBucket,
      pipelineTempBucket: storage.pipelineTempBucket,
      postProcessingBucket: storage.postProcessingBucket,
    });

    const compute = new ComputeConstruct(this, "Compute", {
      appSecurityGroup: networking.appSecurityGroup,
      dbSecret: database.appDbSecret,
      deploymentConfig,
      documentsBucket: storage.documentsBucket,
      pipelineTempBucket: storage.pipelineTempBucket,
      postProcessingBucket: storage.postProcessingBucket,
      taskRole: appIam.taskRole,
      vpc: networking.vpc,
    });

    const batch = new BatchConstruct(this, "Batch", {
      appSecurityGroup: networking.appSecurityGroup,
      dbSecret: database.appDbSecret,
      deploymentConfig,
      documentsBucket: storage.documentsBucket,
      pipelineTempBucket: storage.pipelineTempBucket,
      postProcessingBucket: storage.postProcessingBucket,
      taskRole: appIam.taskRole,
      vpc: networking.vpc,
    });

    const bootstrap = new BootstrapConstruct(this, "Bootstrap", {
      appDbSecret: secrets.appDbSecret,
      appSecurityGroup: networking.appSecurityGroup,
      cluster: batch.cluster,
      deploymentConfig,
      masterDbSecret: secrets.dbSecret,
      repository: batch.repository,
      vpc: networking.vpc,
    });

    new cdk.CfnOutput(this, "DeploymentEnvironment", {
      description: "AISuite deployment environment represented by this stack.",
      value: deploymentConfig.name,
    });

    new cdk.CfnOutput(this, "ProtectedEnvironment", {
      description:
        "Whether deployment should require GitHub environment protection.",
      value: String(deploymentConfig.protectedEnvironment),
    });

    new cdk.CfnOutput(this, "DbEndpoint", {
      description: "RAG PostgreSQL instance endpoint hostname.",
      value: database.endpointAddress,
    });

    new cdk.CfnOutput(this, "DbSecretArn", {
      description:
        "Secrets Manager ARN holding the RAG database master/admin credentials.",
      value: database.secret.secretArn,
    });

    new cdk.CfnOutput(this, "AppDbSecretArn", {
      description:
        "Secrets Manager ARN holding the app-scoped RAG database credentials used by ECS containers.",
      value: database.appDbSecret.secretArn,
    });

    new cdk.CfnOutput(this, "AppTaskRoleArn", {
      description: "ECS task role for the AISuite RAG application.",
      value: appIam.taskRole.roleArn,
    });

    new cdk.CfnOutput(this, "DocumentsBucketName", {
      description: "Source contract documents bucket.",
      value: storage.documentsBucket.bucketName,
    });

    new cdk.CfnOutput(this, "PostProcessingBucketName", {
      description: "Bedrock Data Automation / RAG split output bucket.",
      value: storage.postProcessingBucket.bucketName,
    });

    new cdk.CfnOutput(this, "PipelineCodeBucketName", {
      description: "LLM pipeline code and job artifact bucket.",
      value: storage.pipelineCodeBucket.bucketName,
    });

    new cdk.CfnOutput(this, "PipelineTempBucketName", {
      description: "LLM pipeline scratch bucket.",
      value: storage.pipelineTempBucket.bucketName,
    });

    new cdk.CfnOutput(this, "AlbDnsName", {
      description: "Internal ALB hostname fronting the RAG API.",
      value: compute.loadBalancer.loadBalancerDnsName,
    });

    new cdk.CfnOutput(this, "ApiClusterName", {
      description: "ECS cluster running the RAG API service.",
      value: compute.cluster.clusterName,
    });

    new cdk.CfnOutput(this, "ApiServiceName", {
      description: "ECS Fargate service serving the RAG API.",
      value: compute.service.serviceName,
    });

    new cdk.CfnOutput(this, "ApiEcrRepositoryUri", {
      description: "ECR repository holding the RAG API container image.",
      value: compute.repository.repositoryUri,
    });

    new cdk.CfnOutput(this, "BatchClusterName", {
      description: "ECS cluster for on-demand RAG batch tasks.",
      value: batch.cluster.clusterName,
    });

    new cdk.CfnOutput(this, "PreProcessingTaskDefinitionArn", {
      description: "Task definition ARN for document pre-processing.",
      value: batch.preProcessingTaskDefinition.taskDefinitionArn,
    });

    new cdk.CfnOutput(this, "PreProcessingTaskDefinitionFamily", {
      description: "Task definition family for document pre-processing.",
      value: batch.preProcessingTaskDefinition.family,
    });

    new cdk.CfnOutput(this, "RagProcessTaskDefinitionArn", {
      description: "Task definition ARN for RAG embedding processing.",
      value: batch.ragProcessTaskDefinition.taskDefinitionArn,
    });

    new cdk.CfnOutput(this, "RagProcessTaskDefinitionFamily", {
      description: "Task definition family for RAG embedding processing.",
      value: batch.ragProcessTaskDefinition.family,
    });

    new cdk.CfnOutput(this, "BatchEcrRepositoryUri", {
      description: "ECR repository holding the RAG batch container image.",
      value: batch.repository.repositoryUri,
    });

    new cdk.CfnOutput(this, "BootstrapClusterName", {
      description: "ECS cluster for the one-shot database bootstrap task.",
      value: bootstrap.cluster.clusterName,
    });

    new cdk.CfnOutput(this, "BootstrapTaskDefinitionArn", {
      description: "Task definition ARN for the database bootstrap task.",
      value: bootstrap.taskDefinition.taskDefinitionArn,
    });

    new cdk.CfnOutput(this, "BootstrapSecurityGroupId", {
      description: "Security group for the database bootstrap task.",
      value: bootstrap.securityGroup.securityGroupId,
    });

    new cdk.CfnOutput(this, "BootstrapSubnetIds", {
      description:
        "Comma-separated private subnet IDs for the database bootstrap task.",
      value: cdk.Fn.join(",", bootstrap.subnetIds),
    });
  }
}
