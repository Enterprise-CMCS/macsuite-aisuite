import * as cdk from "aws-cdk-lib";
import type * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";

import { AppIamConstruct } from "./constructs/app-iam-construct";
import { ComputeConstruct } from "./constructs/compute-construct";
import { DatabaseConstruct } from "./constructs/database-construct";
import { EmbeddingsLambdaConstruct } from "./constructs/embeddings-lambda-construct";
import {
  IngestWorkflowConstruct,
  MANIFEST_RUN_PREFIXES,
} from "./constructs/ingest-workflow-construct";
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

    const compute = new ComputeConstruct(this, "Compute", {
      appSecurityGroup: networking.appSecurityGroup,
      dbSecret: database.appDbSecret,
      deploymentConfig,
      documentsBucket: storage.documentsBucket,
      pipelineTempBucket: storage.pipelineTempBucket,
      taskRole: appIam.taskRole,
      vpc: networking.vpc,
    });

    const ingestWorkflow = new IngestWorkflowConstruct(this, "IngestWorkflow", {
      cluster: compute.cluster,
      deploymentConfig,
      embeddingsFunction: embeddings.function,
      preProcessingFunction: preProcessing.function,
      reviewContainer: compute.reviewContainer,
      reviewSecurityGroup: networking.appSecurityGroup,
      reviewTaskDefinition: compute.reviewTaskDefinition,
    });
    compute.apiContainer.addEnvironment(
      "INGEST_STATE_MACHINE_ARN",
      ingestWorkflow.stateMachine.stateMachineArn,
    );

    // Shared by the manifest API and the requirement review task.
    storage.documentsBucket.grantRead(appIam.taskRole);
    for (const prefix of MANIFEST_RUN_PREFIXES) {
      storage.pipelineTempBucket.grantReadWrite(appIam.taskRole, `${prefix}*`);
    }
    ingestWorkflow.stateMachine.grantStartExecution(appIam.taskRole);
    ingestWorkflow.stateMachine.grantRead(appIam.taskRole);

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
        "Lambda the manifest workflow invokes per document; writes RAG_Output/ JSON.",
      value: preProcessing.function.functionName,
    });

    new cdk.CfnOutput(this, "EmbeddingsFunctionName", {
      description:
        "Lambda the manifest workflow invokes to build the versioned per-state embeddings table.",
      value: embeddings.function.functionName,
    });

    new cdk.CfnOutput(this, "IngestStateMachineArn", {
      description:
        "Step Functions state machine running a manifest: pre-processing, embeddings, requirement review and MuleSoft notification.",
      value: ingestWorkflow.stateMachine.stateMachineArn,
    });

    new cdk.CfnOutput(this, "RequirementReviewTaskDefinitionArn", {
      description:
        "Fargate task definition the manifest workflow runs to review legal requirements.",
      value: compute.reviewTaskDefinition.taskDefinitionArn,
    });

    new cdk.CfnOutput(this, "ManifestNotificationQueueUrl", {
      description:
        "SQS queue MuleSoft polls for COMPLETED/FAILED manifest run notifications.",
      value: ingestWorkflow.notificationQueue.queueUrl,
    });

    new cdk.CfnOutput(this, "ManifestNotificationQueueArn", {
      description:
        "ARN of the manifest notification queue, for granting MuleSoft consume access.",
      value: ingestWorkflow.notificationQueue.queueArn,
    });

    new cdk.CfnOutput(this, "ManifestNotificationDeadLetterQueueUrl", {
      description:
        "Dead-letter queue for manifest notifications MuleSoft failed to process.",
      value: ingestWorkflow.notificationDeadLetterQueue.queueUrl,
    });

    new cdk.CfnOutput(this, "AppTaskRoleArn", {
      description:
        "ECS task role shared by the RAG API and the requirement review task.",
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
