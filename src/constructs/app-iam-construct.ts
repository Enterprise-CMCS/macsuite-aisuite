import * as cdk from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
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

export interface AppIamConstructProps {
  dbSecret: secretsmanager.ISecret;
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
      description: "AISuite RAG search API task role.",
    });

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
        resources: CROSS_REGION_INFERENCE_REGIONS.map(
          (inferenceRegion) =>
            `arn:aws:bedrock:${inferenceRegion}::foundation-model/${BEDROCK_RERANK_MODEL}`,
        ),
        sid: "BedrockRerank",
      }),
    );
  }
}
