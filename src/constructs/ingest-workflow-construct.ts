import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as kms from "aws-cdk-lib/aws-kms";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import { Construct } from "constructs";

import { APPLICATION_NAME, type DeploymentConfig } from "../deployment-config";
import { INGEST_MAX_CONCURRENCY } from "./pre-processing-lambda-construct";


export const MANIFEST_RUN_PREFIXES = ["manifests/", "manifest-results/"] as const;
export const TERMINAL_EXECUTION_STATUSES = [
  "FAILED",
  "TIMED_OUT",
  "ABORTED",
] as const;

export const TERMINAL_NOTIFICATION_HANDLER = `const { DescribeExecutionCommand, SFNClient } = require("@aws-sdk/client-sfn");
const { SQSClient, SendMessageCommand } = require("@aws-sdk/client-sqs");

const DEFAULT_ERRORS = {
  ABORTED: "ExecutionAborted",
  FAILED: "ExecutionFailed",
  TIMED_OUT: "States.Timeout",
};

const sfn = new SFNClient({});
const sqs = new SQSClient({});

exports.handler = async (event) => {
  const detail = event?.detail ?? {};
  let rawInput = detail.input;
  let status = detail.status;
  let error = detail.error;

  if (status && !DEFAULT_ERRORS[status]) {
    return { skipped: true, status };
  }

  const missingInput = !rawInput || detail.inputDetails?.included === false;
  const missingFailureDetails = !status || (status === "FAILED" && !error);
  if (detail.executionArn && (missingInput || missingFailureDetails)) {
    const described = await sfn.send(
      new DescribeExecutionCommand({ executionArn: detail.executionArn }),
    );
    rawInput = rawInput || described.input;
    status = status || described.status;
    error = error || described.error;
  }

  if (!DEFAULT_ERRORS[status]) {
    return { skipped: true, status };
  }

  let input = {};
  if (typeof rawInput === "string" && rawInput.length > 0) {
    try {
      input = JSON.parse(rawInput);
    } catch {
      input = {};
    }
  }

  const requestKey =
    input.requestKey ||
    detail.name ||
    (typeof detail.executionArn === "string" ? detail.executionArn.split(":")?.pop() : undefined);
  if (!requestKey) {
    throw new Error("Cannot determine requestKey for execution " + (detail.executionArn ?? "unknown"));
  }

  const body = {
    requestKey,
    ...(input.state !== undefined ? { state: input.state } : {}),
    ...(input.reviewType !== undefined ? { reviewType: input.reviewType } : {}),
    status: "FAILED",
    error: error || DEFAULT_ERRORS[status],
    statusPath: "/manifest/" + requestKey,
  };

  await sqs.send(
    new SendMessageCommand({
      MessageBody: JSON.stringify(body),
      QueueUrl: process.env.NOTIFICATION_QUEUE_URL,
    }),
  );

  return body;
};
`;


const WORKFLOW_TIMEOUT = cdk.Duration.hours(24);
const REVIEW_TASK_TIMEOUT = cdk.Duration.hours(20);


const RUN_INPUT = "$states.context.Execution.Input";
const REQUEST_KEY = `${RUN_INPUT}.requestKey`;

const jsonata = (expression: string): string => `{% ${expression} %}`;

export interface IngestWorkflowConstructProps {
  cluster: ecs.ICluster;
  deploymentConfig: DeploymentConfig;
  embeddingsFunction: lambda.IFunction;
  preProcessingFunction: lambda.IFunction;
  reviewContainer: ecs.ContainerDefinition;
  reviewSecurityGroup: ec2.ISecurityGroup;
  reviewTaskDefinition: ecs.TaskDefinition;
}

interface LambdaStepOptions {
  assign?: Record<string, string>;
  comment: string;
  payload?: Record<string, string>;
}


export class IngestWorkflowConstruct extends Construct {
  public readonly notificationDeadLetterQueue: sqs.IQueue;
  public readonly notificationQueue: sqs.IQueue;
  public readonly stateMachine: sfn.StateMachine;
  public readonly terminalExecutionRule: events.Rule;
  public readonly terminalNotificationFunction: lambda.Function;

  public constructor(
    scope: Construct,
    id: string,
    props: IngestWorkflowConstructProps,
  ) {
    super(scope, id);

    const { manifestNotificationQueue, name, protectedEnvironment } =
      props.deploymentConfig;
    const prefix = `${APPLICATION_NAME}-${name}`;
    const stateMachineName = `${prefix}-manifest-ingest`;
    const removalPolicy = protectedEnvironment
      ? cdk.RemovalPolicy.RETAIN
      : cdk.RemovalPolicy.DESTROY;

    const stack = cdk.Stack.of(this);
    const externalNotificationKey = manifestNotificationQueue?.kmsKeyArn
      ? kms.Key.fromKeyArn(
          this,
          "NotificationQueueKey",
          manifestNotificationQueue.kmsKeyArn,
        )
      : undefined;

    if (manifestNotificationQueue) {
      this.notificationDeadLetterQueue = sqs.Queue.fromQueueArn(
        this,
        "NotificationDeadLetterQueue",
        stack.formatArn({
          resource: manifestNotificationQueue.deadLetterQueueName,
          service: "sqs",
        }),
      );
      this.notificationQueue = sqs.Queue.fromQueueArn(
        this,
        "NotificationQueue",
        stack.formatArn({
          resource: manifestNotificationQueue.queueName,
          service: "sqs",
        }),
      );
    } else {
      this.notificationDeadLetterQueue = new sqs.Queue(
        this,
        "NotificationDeadLetterQueue",
        {
          encryption: sqs.QueueEncryption.SQS_MANAGED,
          enforceSSL: true,
          queueName: `${prefix}-manifest-notifications-dlq`,
          removalPolicy,
          retentionPeriod: cdk.Duration.days(14),
        },
      );
      this.notificationQueue = new sqs.Queue(this, "NotificationQueue", {
        deadLetterQueue: {
          maxReceiveCount: 5,
          queue: this.notificationDeadLetterQueue,
        },
        encryption: sqs.QueueEncryption.SQS_MANAGED,
        enforceSSL: true,
        queueName: `${prefix}-manifest-notifications`,
        removalPolicy,
        retentionPeriod: cdk.Duration.days(14),
        visibilityTimeout: cdk.Duration.minutes(5),
      });
    }

    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/aws/vendedlogs/states/${stateMachineName}`,
      removalPolicy,
      retention: logs.RetentionDays.ONE_MONTH,
    });

    const runFailed = sfn.Fail.jsonata(this, "ManifestRunFailed", {
      cause: jsonata(
        "$exists($failure.Cause) ? $failure.Cause : 'See the execution history.'",
      ),
      error: jsonata(
        "$exists($failure.Error) ? $failure.Error : 'ManifestRunFailed'",
      ),
    });
    const notifyFailed = this.notify(
      "NotifyFailed",
      "FAILED",
      `"error": $failure.Error, "statusPath": "/manifest/" & ${REQUEST_KEY}`,
    );

    notifyFailed.addCatch(runFailed, { errors: [sfn.Errors.ALL] });
    notifyFailed.next(runFailed);

    const preProcessDocuments = sfn.Map.jsonata(this, "PreProcessDocuments", {
      assign: { ragOutputKeys: jsonata("[$states.result.rag_output]") },
      comment:
        "Pre-process the document files.",
      itemSelector: {
        documentId: jsonata("$states.context.Map.Item.Value.documentId"),
        requestKey: jsonata(REQUEST_KEY),
        s3: jsonata("$states.context.Map.Item.Value.s3"),
      },
      items: sfn.ProvideItems.jsonata(jsonata("$states.input.documents")),
      maxConcurrency: INGEST_MAX_CONCURRENCY,
    });
    preProcessDocuments.itemProcessor(
      this.invokeLambda("PreProcessDocument", props.preProcessingFunction, {
        comment:
          "Run Bedrock Data Automation.",
      }),
    );

    const createEmbeddingsTable = this.invokeLambda(
      "CreateEmbeddingsTable",
      props.embeddingsFunction,
      {
        assign: {
          embeddingsTable: jsonata("$states.result.Payload.tableName"),
        },
        comment:
          "Create this embeddings table.",
        payload: {
          action: "create_table",
          requestKey: jsonata(REQUEST_KEY),
          state: jsonata(`${RUN_INPUT}.state`),
        },
      },
    );

    const embedDocuments = sfn.Map.jsonata(this, "EmbedDocuments", {
      comment: "Embed every RAG_Output JSON into this run's embeddings table.",
      itemSelector: {
        action: "embed",
        key: jsonata("$states.context.Map.Item.Value"),
        tableName: jsonata("$embeddingsTable"),
      },
      items: sfn.ProvideItems.jsonata(jsonata("$ragOutputKeys")),
      maxConcurrency: INGEST_MAX_CONCURRENCY,
      outputs: {
        embeddedCount: jsonata("$sum([$states.result.embedded])"),
      },
    });
    embedDocuments.itemProcessor(
      this.invokeLambda("EmbedDocument", props.embeddingsFunction, {
        comment:
          "Embed one RAG_Output JSON, replacing rows left by an earlier attempt.",
      }),
    );

    const noEmbeddingsProduced = sfn.Pass.jsonata(
      this,
      "NoEmbeddingsProduced",
      {
        assign: {
          failure: {
            Cause: jsonata(
              "'Embeddings table ' & $embeddingsTable & ' is empty; no document produced searchable text.'",
            ),
            Error: "NoEmbeddingsProduced",
          },
        },
      },
    );
    noEmbeddingsProduced.next(notifyFailed);

    const { containerName } = props.reviewContainer;
    const reviewRequirements = tasks.EcsRunTask.jsonata(
      this,
      "ReviewRequirements",
      {
        cluster: props.cluster,
        comment:
          "Running requirement for CRT document.",
        containerOverrides: [
          {
            containerDefinition: props.reviewContainer,
            environment: [
              { name: "REQUEST_KEY", value: jsonata(REQUEST_KEY) },
              {
                name: "EMBEDDINGS_TABLE_NAME",
                value: jsonata("$embeddingsTable"),
              },
            ],
          },
        ],
        integrationPattern: sfn.IntegrationPattern.RUN_JOB,
        launchTarget: new tasks.EcsFargateLaunchTarget({
          platformVersion: ecs.FargatePlatformVersion.LATEST,
        }),
        outputs: {
          exitCode: jsonata(
            `$states.result.Containers[Name = '${containerName}'].ExitCode`,
          ),
        },
        securityGroups: [props.reviewSecurityGroup],
        subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
        taskDefinition: props.reviewTaskDefinition,
        taskTimeout: sfn.Timeout.duration(REVIEW_TASK_TIMEOUT),
      },
    );
    // The task checkpoints each review, so a retry resumes instead of starting over.
    reviewRequirements.addRetry({
      backoffRate: 2,
      errors: [sfn.Errors.TASKS_FAILED],
      interval: cdk.Duration.minutes(1),
      maxAttempts: 2,
    });

    const hasEmbeddings = sfn.Choice.jsonata(this, "HasEmbeddings")
      .when(
        sfn.Condition.jsonata(jsonata("$states.input.embeddedCount > 0")),
        reviewRequirements,
      )
      .otherwise(noEmbeddingsProduced);

    const reviewExitedWithError = sfn.Pass.jsonata(
      this,
      "ReviewExitedWithError",
      {
        assign: {
          failure: {
            Cause: jsonata(
              "'Requirement review task exited with code ' & $string($states.input.exitCode) & '; see its CloudWatch logs.'",
            ),
            Error: "RequirementReviewFailed",
          },
        },
      },
    );
    reviewExitedWithError.next(notifyFailed);

    const notifyCompleted = this.notify(
      "NotifyCompleted",
      "COMPLETED",
      [
        `"requirementCount": ${RUN_INPUT}.requirementCount`,
        `"embeddingsTable": $embeddingsTable`,
        `"resultsPath": "/manifest/" & ${REQUEST_KEY} & "/results"`,
      ].join(", "),
    );

    // A stopped task is not necessarily a successful one: only exit code 0 has written results.
    const reviewSucceeded = sfn.Choice.jsonata(this, "ReviewSucceeded")
      .when(
        sfn.Condition.jsonata(jsonata("$states.input.exitCode = 0")),
        notifyCompleted,
      )
      .otherwise(reviewExitedWithError);

    for (const step of [
      preProcessDocuments,
      createEmbeddingsTable,
      embedDocuments,
      reviewRequirements,
    ]) {
      step.addCatch(notifyFailed, {
        assign: { failure: jsonata("$states.errorOutput") },
        errors: [sfn.Errors.ALL],
      });
    }

    reviewRequirements.next(reviewSucceeded);

    const definition = preProcessDocuments
      .next(createEmbeddingsTable)
      .next(embedDocuments)
      .next(hasEmbeddings);

    this.stateMachine = new sfn.StateMachine(this, "StateMachine", {
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      logs: {
        destination: logGroup,
        includeExecutionData: false,
        level: sfn.LogLevel.ERROR,
      },
      queryLanguage: sfn.QueryLanguage.JSONATA,
      stateMachineName,
      stateMachineType: sfn.StateMachineType.STANDARD,
      timeout: WORKFLOW_TIMEOUT,
      tracingEnabled: true,
    });

    const terminalNotifierName = `${prefix}-manifest-terminal-notify`;
    const terminalNotifierLogGroup = new logs.LogGroup(
      this,
      "TerminalNotificationLogGroup",
      {
        logGroupName: `/aws/lambda/${terminalNotifierName}`,
        removalPolicy,
        retention: logs.RetentionDays.ONE_MONTH,
      },
    );

    this.terminalNotificationFunction = new lambda.Function(
      this,
      "TerminalNotificationFunction",
      {
        architecture: lambda.Architecture.ARM_64,
        code: lambda.Code.fromInline(TERMINAL_NOTIFICATION_HANDLER),
        description:
          "Sends a FAILED MuleSoft SQS notification when a manifest workflow execution ends in FAILED, TIMED_OUT, or ABORTED.",
        environment: {
          NOTIFICATION_QUEUE_URL: this.notificationQueue.queueUrl,
        },
        functionName: terminalNotifierName,
        handler: "index.handler",
        logGroup: terminalNotifierLogGroup,
        runtime: lambda.Runtime.NODEJS_22_X,
        timeout: cdk.Duration.seconds(30),
      },
    );
    this.notificationQueue.grantSendMessages(this.terminalNotificationFunction);
    this.stateMachine.grantRead(this.terminalNotificationFunction);

    if (externalNotificationKey) {
      for (const grantee of [
        this.stateMachine,
        this.terminalNotificationFunction,
      ]) {
        externalNotificationKey.grant(
          grantee,
          "kms:Decrypt",
          "kms:GenerateDataKey*",
        );
      }
    }

    this.terminalExecutionRule = new events.Rule(
      this,
      "TerminalExecutionRule",
      {
        description:
          "Routes FAILED, TIMED_OUT, and ABORTED manifest workflow executions to the MuleSoft failure notifier.",
        eventPattern: {
          detail: {
            stateMachineArn: [this.stateMachine.stateMachineArn],
            status: [...TERMINAL_EXECUTION_STATUSES],
          },
          detailType: ["Step Functions Execution Status Change"],
          source: ["aws.states"],
        },
        ruleName: `${prefix}-manifest-terminal-status`,
        targets: [
          new targets.LambdaFunction(this.terminalNotificationFunction, {
            maxEventAge: cdk.Duration.hours(24),
            retryAttempts: 3,
          }),
        ],
      },
    );
  }

  private invokeLambda(
    id: string,
    lambdaFunction: lambda.IFunction,
    options: LambdaStepOptions,
  ): tasks.LambdaInvoke {
    const step = tasks.LambdaInvoke.jsonata(this, id, {
      assign: options.assign,
      comment: options.comment,
      lambdaFunction,
      outputs: jsonata("$states.result.Payload"),
      payload: options.payload
        ? sfn.TaskInput.fromObject(options.payload)
        : undefined,
      retryOnServiceExceptions: true,
    });
    // Reserved concurrency throttles any burst above INGEST_MAX_CONCURRENCY.
    step.addRetry({
      backoffRate: 2,
      errors: ["Lambda.TooManyRequestsException"],
      interval: cdk.Duration.seconds(5),
      jitterStrategy: sfn.JitterType.FULL,
      maxAttempts: 8,
      maxDelay: cdk.Duration.minutes(2),
    });
    // Transient BDA, Bedrock and database errors; every Lambda action is safe to repeat.
    step.addRetry({
      backoffRate: 2,
      errors: [sfn.Errors.TASKS_FAILED],
      interval: cdk.Duration.seconds(30),
      maxAttempts: 2,
    });
    return step;
  }

  /** MuleSoft notification; Standard queues deliver at least once, so consumers dedupe on requestKey + status. */
  private notify(
    id: string,
    status: "COMPLETED" | "FAILED",
    fields: string,
  ): tasks.SqsSendMessage {
    const step = tasks.SqsSendMessage.jsonata(this, id, {
      comment: `Notify MuleSoft that the manifest run is ${status}.`,
      messageBody: sfn.TaskInput.fromText(
        jsonata(
          `$string({"requestKey": ${REQUEST_KEY}, "state": ${RUN_INPUT}.state, "reviewType": ${RUN_INPUT}.reviewType, "status": "${status}", ${fields}})`,
        ),
      ),
      queue: this.notificationQueue,
    });
    step.addRetry({
      backoffRate: 2,
      errors: [sfn.Errors.TASKS_FAILED],
      interval: cdk.Duration.seconds(5),
      maxAttempts: 3,
    });
    return step;
  }
}
