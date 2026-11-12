import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { describe, expect, it } from "vitest";

import { REVIEW_CONTAINER_NAME } from "../src/constructs/compute-construct";
import { IngestWorkflowConstruct } from "../src/constructs/ingest-workflow-construct";
import { getDeploymentConfig } from "../src/deployment-config";

interface StateJson {
  Catch?: Array<{
    Assign?: Record<string, unknown>;
    ErrorEquals: string[];
    Next: string;
  }>;
  Choices?: Array<{ Next: string }>;
  Default?: string;
  End?: boolean;
  ItemProcessor?: { StartAt: string };
  MaxConcurrency?: number;
  Next?: string;
  Output?: Record<string, unknown>;
  Resource?: string;
  Type: string;
}

interface DefinitionJson {
  QueryLanguage: string;
  StartAt: string;
  States: Record<string, StateJson>;
}

const CAUGHT_STEPS = [
  "PreProcessDocuments",
  "AllocateEmbeddingVersion",
  "EmbedDocuments",
  "FinalizeEmbeddingVersion",
  "ReviewRequirements",
] as const;

function synthesize(): Template {
  const app = new cdk.App();
  const deploymentConfig = getDeploymentConfig("dev");
  const stack = new cdk.Stack(app, "IngestWorkflowTestStack", {
    env: deploymentConfig.awsEnvironment,
  });
  const vpc = new ec2.Vpc(stack, "Vpc", { maxAzs: 2, natGateways: 1 });
  const reviewTaskDefinition = new ecs.FargateTaskDefinition(
    stack,
    "ReviewTask",
  );
  const reviewContainer = reviewTaskDefinition.addContainer("Review", {
    containerName: REVIEW_CONTAINER_NAME,
    image: ecs.ContainerImage.fromRegistry("python:3.12-slim"),
  });
  const inlineFunction = (id: string): lambda.Function =>
    new lambda.Function(stack, id, {
      code: lambda.Code.fromInline("exports.handler = async () => ({});"),
      handler: "index.handler",
      runtime: lambda.Runtime.NODEJS_22_X,
    });

  new IngestWorkflowConstruct(stack, "IngestWorkflow", {
    cluster: new ecs.Cluster(stack, "Cluster", { vpc }),
    deploymentConfig,
    embeddingsFunction: inlineFunction("Embeddings"),
    preProcessingFunction: inlineFunction("PreProcessing"),
    reviewContainer,
    reviewSecurityGroup: new ec2.SecurityGroup(stack, "Sg", { vpc }),
    reviewTaskDefinition,
  });

  return Template.fromStack(stack);
}

/** Renders the ASL with every CloudFormation token replaced by a placeholder. */
function definitionOf(template: Template): DefinitionJson {
  const [stateMachine] = Object.values(
    template.findResources("AWS::StepFunctions::StateMachine"),
  );
  const [, parts] = stateMachine?.Properties?.DefinitionString[
    "Fn::Join"
  ] as [string, unknown[]];

  return JSON.parse(
    parts.map((part) => (typeof part === "string" ? part : "TOKEN")).join(""),
  ) as DefinitionJson;
}

describe("IngestWorkflowConstruct", () => {
  const template = synthesize();
  const definition = definitionOf(template);
  const states = definition.States;

  it("runs a logged, traced JSONata Standard state machine", () => {
    template.hasResourceProperties("AWS::StepFunctions::StateMachine", {
      StateMachineName: "aisuite-dev-manifest-ingest",
      StateMachineType: "STANDARD",
      TracingConfiguration: { Enabled: true },
      LoggingConfiguration: Match.objectLike({
        IncludeExecutionData: false,
        Level: "ERROR",
      }),
    });
    expect(definition.QueryLanguage).toBe("JSONata");
  });

  it("embeds only after every document is pre-processed, then reviews on Fargate", () => {
    expect(definition.StartAt).toBe("PreProcessDocuments");
    expect(states.PreProcessDocuments?.MaxConcurrency).toBe(5);
    expect(states.PreProcessDocuments?.ItemProcessor?.StartAt).toBe(
      "PreProcessDocument",
    );
    expect(states.PreProcessDocuments?.Next).toBe("AllocateEmbeddingVersion");
    expect(states.AllocateEmbeddingVersion?.Next).toBe("EmbedDocuments");
    expect(states.EmbedDocuments?.ItemProcessor?.StartAt).toBe("EmbedDocument");
    expect(states.EmbedDocuments?.Next).toBe("FinalizeEmbeddingVersion");
    expect(states.FinalizeEmbeddingVersion?.Next).toBe("ReviewRequirements");
    expect(states.ReviewRequirements?.Resource).toContain("ecs:runTask.sync");
    expect(states.ReviewRequirements?.Output?.exitCode).toContain(
      `Containers[Name = '${REVIEW_CONTAINER_NAME}'].ExitCode`,
    );
    expect(states.ReviewRequirements?.Next).toBe("ReviewSucceeded");
  });

  it("notifies MuleSoft only when the review task exits cleanly", () => {
    expect(states.ReviewSucceeded?.Choices?.[0]?.Next).toBe("NotifyCompleted");
    expect(states.ReviewSucceeded?.Default).toBe("ReviewExitedWithError");
    expect(states.ReviewExitedWithError?.Next).toBe(
      "MarkEmbeddingVersionFailed",
    );
    expect(states.NotifyCompleted?.Resource).toContain("sqs:sendMessage");
    expect(states.NotifyCompleted?.End).toBe(true);
  });

  it("routes every failure through version cleanup and a FAILED notification", () => {
    for (const step of CAUGHT_STEPS) {
      expect(states[step]?.Catch).toEqual([
        {
          Assign: { failure: "{% $states.errorOutput %}" },
          ErrorEquals: ["States.ALL"],
          Next: "MarkEmbeddingVersionFailed",
        },
      ]);
    }
    expect(states.MarkEmbeddingVersionFailed?.Next).toBe("NotifyFailed");
    expect(states.MarkEmbeddingVersionFailed?.Catch?.[0]?.Next).toBe(
      "NotifyFailed",
    );
    expect(states.NotifyFailed?.Next).toBe("ManifestRunFailed");
    expect(states.NotifyFailed?.Catch?.[0]?.Next).toBe("ManifestRunFailed");
    expect(states.ManifestRunFailed?.Type).toBe("Fail");
  });

  it("creates an encrypted notification queue with a dead-letter queue", () => {
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "aisuite-dev-manifest-notifications",
      RedrivePolicy: Match.objectLike({ maxReceiveCount: 5 }),
      SqsManagedSseEnabled: true,
    });
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "aisuite-dev-manifest-notifications-dlq",
      SqsManagedSseEnabled: true,
    });
  });
});
