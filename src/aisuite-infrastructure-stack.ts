import * as cdk from "aws-cdk-lib";
import type * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";

import { AppIamConstruct } from "./constructs/app-iam-construct";
import { ComputeConstruct } from "./constructs/compute-construct";
import { DatabaseConstruct } from "./constructs/database-construct";
import { EmbeddingsLambdaConstruct } from "./constructs/embeddings-lambda-construct";
import { NetworkingConstruct } from "./constructs/networking-construct";
import { PreProcessingLambdaConstruct } from "./constructs/pre-processing-lambda-construct";
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
    });

    const compute = new ComputeConstruct(this, "Compute", {
      appSecurityGroup: networking.appSecurityGroup,
      dbSecret: database.appDbSecret,
      deploymentConfig,
      taskRole: appIam.taskRole,
      vpc: networking.vpc,
    });

    const preProcessing = new PreProcessingLambdaConstruct(
      this,
      "PreProcessingLambda",
      {
        appSecurityGroup: networking.appSecurityGroup,
        deploymentConfig,
        documentsBucket: storage.documentsBucket,
        pipelineTempBucket: storage.pipelineTempBucket,
        postProcessingBucket: storage.postProcessingBucket,
        vpc: networking.vpc,
      },
    );

    const embeddings = new EmbeddingsLambdaConstruct(this, "EmbeddingsLambda", {
      appSecurityGroup: networking.appSecurityGroup,
      dbSecret: database.appDbSecret,
      deploymentConfig,
      postProcessingBucket: storage.postProcessingBucket,
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
        "Secrets Manager ARN holding the app-scoped RAG database credentials.",
      value: database.appDbSecret.secretArn,
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

    new cdk.CfnOutput(this, "PreProcessingFunctionName", {
      description:
        "Lambda triggered by each PDF/DOCX uploaded to the documents bucket; writes RAG_Output/ JSON.",
      value: preProcessing.function.functionName,
    });

    new cdk.CfnOutput(this, "EmbeddingsFunctionName", {
      description:
        "Lambda triggered by each RAG_Output/ JSON written to the post-processing bucket; stores embeddings.",
      value: embeddings.function.functionName,
    });

    new cdk.CfnOutput(this, "AppTaskRoleArn", {
      description: "ECS task role for the AISuite RAG search API.",
      value: appIam.taskRole.roleArn,
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
  }
}
