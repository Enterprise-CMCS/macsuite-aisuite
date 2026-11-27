import vm from "node:vm";

import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { describe, expect, it, vi } from "vitest";

import { REVIEW_CONTAINER_NAME } from "../src/constructs/compute-construct";
import {
  IngestWorkflowConstruct,
  LAMBDA_THROTTLE_RETRY_INTERVAL,
  LAMBDA_THROTTLE_RETRY_MAX_ATTEMPTS,
  LAMBDA_THROTTLE_RETRY_MAX_DELAY,
  TERMINAL_EXECUTION_STATUSES,
  TERMINAL_NOTIFICATION_HANDLER,
} from "../src/constructs/ingest-workflow-construct";
import { INGEST_TIMEOUT } from "../src/constructs/pre-processing-lambda-construct";
import { getDeploymentConfig } from "../src/deployment-config";

interface RetryJson {
  BackoffRate?: number;
  ErrorEquals: string[];
  IntervalSeconds?: number;
  JitterStrategy?: string;
  MaxAttempts?: number;
  MaxDelaySeconds?: number;
}

interface StateJson {
  Catch?: Array<{
    Assign?: Record<string, unknown>;
    ErrorEquals: string[];
    Next: string;
  }>;
  Choices?: Array<{ Next: string }>;
  Default?: string;
  End?: boolean;
  ItemProcessor?: { StartAt: string; States?: Record<string, StateJson> };
  ItemSelector?: Record<string, unknown>;
  MaxConcurrency?: number;
  Next?: string;
  Output?: Record<string, unknown>;
  Resource?: string;
  Retry?: RetryJson[];
  Type: string;
}

interface DefinitionJson {
  QueryLanguage: string;
  StartAt: string;
  States: Record<string, StateJson>;
}

const CAUGHT_STEPS = [
  "PreProcessDocuments",
  "CreateEmbeddingsTable",
  "EmbedDocuments",
  "ReviewRequirements",
] as const;

function synthesize(
  environmentName: "dev" | "qa" = "dev",
): Template {
  const app = new cdk.App();
  const deploymentConfig = getDeploymentConfig(environmentName);
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

  it("embeds only after every document is pre-processed, checks non-empty output, then reviews on Fargate", () => {
    expect(definition.StartAt).toBe("PreProcessDocuments");
    expect(states.PreProcessDocuments?.MaxConcurrency).toBe(5);
    expect(states.PreProcessDocuments?.ItemSelector).toEqual({
      documentId: "{% $states.context.Map.Item.Value.documentId %}",
      requestKey: "{% $states.context.Execution.Input.requestKey %}",
      s3: "{% $states.context.Map.Item.Value.s3 %}",
    });
    expect(states.PreProcessDocuments?.ItemProcessor?.StartAt).toBe(
      "PreProcessDocument",
    );
    expect(states.PreProcessDocuments?.Next).toBe("CreateEmbeddingsTable");
    expect(states.CreateEmbeddingsTable?.Next).toBe("EmbedDocuments");
    expect(states.EmbedDocuments?.ItemProcessor?.StartAt).toBe("EmbedDocument");
    expect(states.EmbedDocuments?.Output?.embeddedCount).toBe(
      "{% $sum([$states.result.embedded]) %}",
    );
    expect(states.EmbedDocuments?.Next).toBe("HasEmbeddings");
    expect(states.HasEmbeddings?.Choices?.[0]?.Next).toBe("ReviewRequirements");
    expect(states.HasEmbeddings?.Default).toBe("NoEmbeddingsProduced");
    expect(states.NoEmbeddingsProduced?.Next).toBe("NotifyFailed");
    expect(states.ReviewRequirements?.Resource).toContain("ecs:runTask.sync");
    expect(states.ReviewRequirements?.Output?.exitCode).toContain(
      `Containers[Name = '${REVIEW_CONTAINER_NAME}'].ExitCode`,
    );
    expect(states.ReviewRequirements?.Next).toBe("ReviewSucceeded");
  });

  it("retries Lambda throttling long enough to outlast competing 900-second manifest runs under FULL jitter", () => {
    const lambdaStates: Array<[string, StateJson | undefined]> = [
      [
        "PreProcessDocument",
        states.PreProcessDocuments?.ItemProcessor?.States?.PreProcessDocument,
      ],
      ["CreateEmbeddingsTable", states.CreateEmbeddingsTable],
      [
        "EmbedDocument",
        states.EmbedDocuments?.ItemProcessor?.States?.EmbedDocument,
      ],
    ];

    for (const [name, state] of lambdaStates) {
      const throttleRetry = state?.Retry?.find((rule) =>
        rule.ErrorEquals.includes("Lambda.TooManyRequestsException"),
      );
      expect(throttleRetry, `${name} throttling retry`).toEqual({
        BackoffRate: 2,
        ErrorEquals: ["Lambda.TooManyRequestsException"],
        IntervalSeconds: LAMBDA_THROTTLE_RETRY_INTERVAL.toSeconds(),
        JitterStrategy: "FULL",
        MaxAttempts: LAMBDA_THROTTLE_RETRY_MAX_ATTEMPTS,
        MaxDelaySeconds: LAMBDA_THROTTLE_RETRY_MAX_DELAY.toSeconds(),
      });

      const interval = throttleRetry!.IntervalSeconds!;
      const backoff = throttleRetry!.BackoffRate!;
      const maxDelay = throttleRetry!.MaxDelaySeconds!;
      const maxAttempts = throttleRetry!.MaxAttempts!;
      let unjitteredTotal = 0;
      for (let attempt = 0; attempt < maxAttempts; attempt++) {
        unjitteredTotal += Math.min(interval * backoff ** attempt, maxDelay);
      }
      // FULL jitter halves expected wait; ensure even the expected wait comfortably exceeds
      // multiple full 900-second Lambda executions from competing manifests.
      const expectedWaitSeconds = unjitteredTotal / 2;
      expect(expectedWaitSeconds).toBeGreaterThan(INGEST_TIMEOUT.toSeconds() * 4);
    }
  });

  it("notifies MuleSoft only when the review task exits cleanly", () => {
    expect(states.ReviewSucceeded?.Choices?.[0]?.Next).toBe("NotifyCompleted");
    expect(states.ReviewSucceeded?.Default).toBe("ReviewExitedWithError");
    expect(states.ReviewExitedWithError?.Next).toBe("NotifyFailed");
    expect(states.NotifyCompleted?.Resource).toContain("sqs:sendMessage");
    expect(states.NotifyCompleted?.End).toBe(true);
  });

  it("routes every failure directly to a FAILED notification", () => {
    for (const step of CAUGHT_STEPS) {
      expect(states[step]?.Catch).toEqual([
        {
          Assign: { failure: "{% $states.errorOutput %}" },
          ErrorEquals: ["States.ALL"],
          Next: "NotifyFailed",
        },
      ]);
    }
    expect(states.MarkEmbeddingVersionFailed).toBeUndefined();
    expect(states.NotifyFailed?.Next).toBe("ManifestRunFailed");
    expect(states.NotifyFailed?.Catch?.[0]?.Next).toBe("ManifestRunFailed");
    expect(states.ManifestRunFailed?.Type).toBe("Fail");
  });

  it("imports the external MuleSoft notification queue and grants KMS access in dev", () => {
    template.resourceCountIs("AWS::SQS::Queue", 0);
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: "sqs:SendMessage",
            Effect: "Allow",
            Resource: {
              "Fn::Join": [
                "",
                [
                  "arn:",
                  { Ref: "AWS::Partition" },
                  ":sqs:us-east-1:205501819586:macsuite-aisuite-dev-mulesoft-sqs-queue",
                ],
              ],
            },
          }),
          Match.objectLike({
            Action: ["kms:Decrypt", "kms:GenerateDataKey*"],
            Effect: "Allow",
            Resource:
              "arn:aws:kms:us-east-1:205501819586:key/626ce0e6-6103-4699-a587-534db33232e2",
          }),
        ]),
      },
    });
  });

  it("creates an encrypted notification queue with a dead-letter queue when no external queue is configured", () => {
    const qaTemplate = synthesize("qa");
    qaTemplate.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "aisuite-qa-manifest-notifications",
      RedrivePolicy: Match.objectLike({ maxReceiveCount: 5 }),
      SqsManagedSseEnabled: true,
    });
    qaTemplate.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "aisuite-qa-manifest-notifications-dlq",
      SqsManagedSseEnabled: true,
    });
  });

  it("routes FAILED, TIMED_OUT, and ABORTED executions through an EventBridge terminal-status notifier", () => {
    expect(TERMINAL_EXECUTION_STATUSES).toEqual([
      "FAILED",
      "TIMED_OUT",
      "ABORTED",
    ]);
    template.hasResourceProperties("AWS::Events::Rule", {
      EventPattern: {
        detail: {
          stateMachineArn: [Match.anyValue()],
          status: [...TERMINAL_EXECUTION_STATUSES],
        },
        "detail-type": ["Step Functions Execution Status Change"],
        source: ["aws.states"],
      },
      Name: "aisuite-dev-manifest-terminal-status",
      State: "ENABLED",
      Targets: [
        Match.objectLike({
          RetryPolicy: {
            MaximumEventAgeInSeconds: 86400,
            MaximumRetryAttempts: 3,
          },
        }),
      ],
    });

    template.hasResourceProperties("AWS::Lambda::Function", {
      Architectures: ["arm64"],
      FunctionName: "aisuite-dev-manifest-terminal-notify",
      Handler: "index.handler",
      Runtime: "nodejs22.x",
      Timeout: 30,
    });
  });

  it("publishes FAILED MuleSoft notifications for FAILED, TIMED_OUT, and ABORTED executions", async () => {
    const sendSfn = vi.fn();
    const sendSqs = vi.fn().mockResolvedValue({});

    class MockDescribeExecutionCommand {
      public readonly input: Record<string, unknown>;
      public constructor(input: Record<string, unknown>) {
        this.input = input;
      }
    }
    class MockSendMessageCommand {
      public readonly input: Record<string, unknown>;
      public constructor(input: Record<string, unknown>) {
        this.input = input;
      }
    }

    const moduleExports: { handler?: (event: unknown) => Promise<unknown> } = {};
    const sandbox = {
      exports: moduleExports,
      process: {
        env: { NOTIFICATION_QUEUE_URL: "https://sqs.us-east-1.amazonaws.com/123456789012/queue" },
      },
      require: (id: string) => {
        if (id === "@aws-sdk/client-sfn") {
          return {
            DescribeExecutionCommand: MockDescribeExecutionCommand,
            SFNClient: class {
              public send = sendSfn;
            },
          };
        }
        if (id === "@aws-sdk/client-sqs") {
          return {
            SQSClient: class {
              public send = sendSqs;
            },
            SendMessageCommand: MockSendMessageCommand,
          };
        }
        throw new Error(`Unexpected module: ${id}`);
      },
    };
    vm.runInNewContext(TERMINAL_NOTIFICATION_HANDLER, sandbox);
    const handler = moduleExports.handler!;

    await handler({
      detail: {
        executionArn:
          "arn:aws:states:us-east-1:123456789012:execution:aisuite-dev-manifest-ingest:req-timeout-1",
        input: JSON.stringify({
          requestKey: "req-timeout-1",
          reviewType: "STREAMLINED",
          state: "MD",
        }),
        inputDetails: { included: true },
        name: "req-timeout-1",
        status: "TIMED_OUT",
      },
    });

    expect(sendSfn).not.toHaveBeenCalled();
    expect(sendSqs).toHaveBeenCalledTimes(1);
    expect(JSON.parse(sendSqs.mock.calls[0]?.[0]?.input?.MessageBody)).toEqual({
      error: "States.Timeout",
      requestKey: "req-timeout-1",
      reviewType: "STREAMLINED",
      state: "MD",
      status: "FAILED",
      statusPath: "/manifest/req-timeout-1",
    });

    sendSqs.mockClear();
    sendSfn.mockResolvedValueOnce({
      input: JSON.stringify({
        requestKey: "req-aborted-2",
        reviewType: "FULL",
        state: "WA",
      }),
      status: "ABORTED",
    });

    await handler({
      detail: {
        executionArn:
          "arn:aws:states:us-east-1:123456789012:execution:aisuite-dev-manifest-ingest:req-aborted-2",
        inputDetails: { included: false },
        name: "req-aborted-2",
        status: "ABORTED",
      },
    });

    expect(sendSfn).toHaveBeenCalledTimes(1);
    expect(JSON.parse(sendSqs.mock.calls[0]?.[0]?.input?.MessageBody)).toEqual({
      error: "ExecutionAborted",
      requestKey: "req-aborted-2",
      reviewType: "FULL",
      state: "WA",
      status: "FAILED",
      statusPath: "/manifest/req-aborted-2",
    });

    sendSfn.mockClear();
    sendSqs.mockClear();
    sendSfn.mockResolvedValueOnce({
      error: "SQS.SdkClientException",
      status: "FAILED",
    });

    await handler({
      detail: {
        executionArn:
          "arn:aws:states:us-east-1:123456789012:execution:aisuite-dev-manifest-ingest:req-failed-3",
        input: JSON.stringify({
          requestKey: "req-failed-3",
          reviewType: "STREAMLINED",
          state: "TN",
        }),
        inputDetails: { included: true },
        name: "req-failed-3",
        status: "FAILED",
      },
    });

    expect(sendSfn).toHaveBeenCalledTimes(1);
    expect(JSON.parse(sendSqs.mock.calls[0]?.[0]?.input?.MessageBody)).toEqual({
      error: "SQS.SdkClientException",
      requestKey: "req-failed-3",
      reviewType: "STREAMLINED",
      state: "TN",
      status: "FAILED",
      statusPath: "/manifest/req-failed-3",
    });

    sendSfn.mockClear();
    sendSqs.mockClear();
    await handler({
      detail: {
        error: "States.Runtime",
        executionArn:
          "arn:aws:states:us-east-1:123456789012:execution:aisuite-dev-manifest-ingest:req-failed-4",
        input: JSON.stringify({
          requestKey: "req-failed-4",
          reviewType: "FULL",
          state: "ME",
        }),
        inputDetails: { included: true },
        name: "req-failed-4",
        status: "FAILED",
      },
    });

    expect(sendSfn).not.toHaveBeenCalled();
    expect(JSON.parse(sendSqs.mock.calls[0]?.[0]?.input?.MessageBody)).toEqual({
      error: "States.Runtime",
      requestKey: "req-failed-4",
      reviewType: "FULL",
      state: "ME",
      status: "FAILED",
      statusPath: "/manifest/req-failed-4",
    });

    sendSfn.mockClear();
    sendSqs.mockClear();
    const skipped = await handler({
      detail: {
        executionArn:
          "arn:aws:states:us-east-1:123456789012:execution:aisuite-dev-manifest-ingest:req-ok-5",
        name: "req-ok-5",
        status: "SUCCEEDED",
      },
    });
    expect(skipped).toEqual({ skipped: true, status: "SUCCEEDED" });
    expect(sendSfn).not.toHaveBeenCalled();
    expect(sendSqs).not.toHaveBeenCalled();
  });
});
