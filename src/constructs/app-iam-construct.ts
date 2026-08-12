import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import type * as s3 from "aws-cdk-lib/aws-s3";
import type * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

const CROSS_REGION_INFERENCE_REGIONS = ["us-east-1", "us-east-2", "us-west-2"];

const BEDROCK_FOUNDATION_MODELS = [
  "amazon.nova-pro-v1:0",
  "cohere.embed-v4:0",
] as const;

const BEDROCK_INFERENCE_PROFILES = [
  "us.amazon.nova-pro-v1:0",
  "us.cohere.embed-v4:0",
] as const;

const BEDROCK_RERANK_MODEL = "cohere.rerank-v3-5:0";
const BEDROCK_RERANK_REGIONS = ["us-east-1", "us-west-2"] as const;

export interface AppIamConstructProps {
  dbSecret: secretsmanager.ISecret;
  documentsBucket: s3.IBucket;
  pipelineTempBucket: s3.IBucket;
  postProcessingBucket: s3.IBucket;
}

export class AppIamConstruct extends Construct {
  public readonly taskRole: iam.Role;

  public constructor(
    scope: Construct,
    id: string,
    props: AppIamConstructProps,
  ) {
    super(scope, id);

    const { account, region } = cdk.Stack.of(this);

    this.taskRole = new iam.Role(this, "TaskRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      description: "AISuite RAG pipeline task role.",
    });

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:ListBucket"],
        resources: [props.documentsBucket.bucketArn],
        sid: "DocumentsBucketList",
      }),
    );
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetObject"],
        resources: [`${props.documentsBucket.bucketArn}/*`],
        sid: "DocumentsBucketRead",
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:ListBucket"],
        resources: [props.postProcessingBucket.bucketArn],
        sid: "PostProcessingBucketList",
      }),
    );
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"],
        resources: [`${props.postProcessingBucket.bucketArn}/*`],
        sid: "PostProcessingBucketAccess",
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:ListBucket"],
        resources: [props.pipelineTempBucket.bucketArn],
        sid: "PipelineTempBucketList",
      }),
    );
    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetObject", "s3:PutObject"],
        resources: [`${props.pipelineTempBucket.bucketArn}/*`],
        sid: "PipelineTempBucketAccess",
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["secretsmanager:GetSecretValue"],
        resources: [props.dbSecret.secretArn],
        sid: "RagAppDatabaseCredentials",
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources: [
          ...CROSS_REGION_INFERENCE_REGIONS.flatMap((inferenceRegion) =>
            BEDROCK_FOUNDATION_MODELS.map(
              (modelId) =>
                `arn:aws:bedrock:${inferenceRegion}::foundation-model/${modelId}`,
            ),
          ),
          ...BEDROCK_INFERENCE_PROFILES.map(
            (profileId) =>
              `arn:aws:bedrock:${region}:${account}:inference-profile/${profileId}`,
          ),
        ],
        sid: "BedrockModelInvocation",
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:Rerank"],
        resources: ["*"],
        sid: "BedrockRerank",
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel"],
        resources: BEDROCK_RERANK_REGIONS.map(
          (inferenceRegion) =>
            `arn:aws:bedrock:${inferenceRegion}::foundation-model/${BEDROCK_RERANK_MODEL}`,
        ),
        sid: "BedrockRerankInvokeModel",
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:CreateDataAutomationProject",
          "bedrock:DeleteDataAutomationProject",
          "bedrock:GetDataAutomationProject",
          "bedrock:UpdateDataAutomationProject",
        ],
        resources: [
          `arn:aws:bedrock:${region}:${account}:data-automation-project/*`,
        ],
        sid: "BedrockDataAutomationProjects",
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:ListDataAutomationProjects"],
        resources: ["*"],
        sid: "BedrockDataAutomationProjectList",
      }),
    );

    this.taskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:GetDataAutomationStatus",
          "bedrock:InvokeDataAutomationAsync",
        ],
        resources: [
          `arn:aws:bedrock:${region}:${account}:data-automation-project/*`,
          `arn:aws:bedrock:${region}:${account}:data-automation-profile/*`,
          `arn:aws:bedrock:${region}:${account}:data-automation-invocation/*`,
        ],
        sid: "BedrockDataAutomationRuntime",
      }),
    );
  }
}
