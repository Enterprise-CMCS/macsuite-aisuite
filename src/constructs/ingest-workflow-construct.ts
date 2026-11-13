import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import type * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import { Construct } from "constructs";

import { APPLICATION_NAME, type DeploymentConfig } from "../deployment-config";
import { INGEST_MAX_CONCURRENCY } from "./pre-processing-lambda-construct";


export const MANIFEST_RUN_PREFIXES = ["manifests/", "manifest-results/"] as const;


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
  public readonly notificationDeadLetterQueue: sqs.Queue;
  public readonly notificationQueue: sqs.Queue;
  public readonly stateMachine: sfn.StateMachine;

  public constructor(
    scope: Construct,
    id: string,
    props: IngestWorkflowConstructProps,
  ) {
    super(scope, id);

    const { name, protectedEnvironment } = props.deploymentConfig;
    const prefix = `${APPLICATION_NAME}-${name}`;
    const stateMachineName = `${prefix}-manifest-ingest`;
    const removalPolicy = protectedEnvironment
      ? cdk.RemovalPolicy.RETAIN
      : cdk.RemovalPolicy.DESTROY;

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
    const markVersionFailed = this.invokeLambda(
      "MarkEmbeddingVersionFailed",
      props.embeddingsFunction,
      {
        comment:
          "Flag this run's unfinished embeddings version as FAILED so it is never searched.",
        payload: { action: "fail", requestKey: jsonata(REQUEST_KEY) },
      },
    );
    
    markVersionFailed.addCatch(notifyFailed, { errors: [sfn.Errors.ALL] });
    markVersionFailed.next(notifyFailed).next(runFailed);

    const preProcessDocuments = sfn.Map.jsonata(this, "PreProcessDocuments", {
      assign: { ragOutputKeys: jsonata("[$states.result.rag_output]") },
      comment:
        "Pre-process every manifest document; embedding starts only after all of them succeed.",
      items: sfn.ProvideItems.jsonata(jsonata("$states.input.documents")),
      maxConcurrency: INGEST_MAX_CONCURRENCY,
    });
    preProcessDocuments.itemProcessor(
      this.invokeLambda("PreProcessDocument", props.preProcessingFunction, {
        comment:
          "Run Bedrock Data Automation for one document and write its RAG_Output JSON.",
      }),
    );

    const allocateVersion = this.invokeLambda(
      "AllocateEmbeddingVersion",
      props.embeddingsFunction,
      {
        assign: {
          embeddingVersion: jsonata("$states.result.Payload.version"),
          embeddingsTable: jsonata("$states.result.Payload.tableName"),
        },
        comment:
          "Reserve and create this run's embeddings_<state>_v<version> table.",
        payload: {
          action: "allocate",
          requestKey: jsonata(REQUEST_KEY),
          state: jsonata(`${RUN_INPUT}.state`),
        },
      },
    );

    const embedDocuments = sfn.Map.jsonata(this, "EmbedDocuments", {
      comment: "Embed every RAG_Output JSON into this run's versioned table.",
      itemSelector: {
        action: "embed",
        key: jsonata("$states.context.Map.Item.Value"),
        tableName: jsonata("$embeddingsTable"),
      },
      items: sfn.ProvideItems.jsonata(jsonata("$ragOutputKeys")),
      maxConcurrency: INGEST_MAX_CONCURRENCY,
    });
    embedDocuments.itemProcessor(
      this.invokeLambda("EmbedDocument", props.embeddingsFunction, {
        comment:
          "Embed one RAG_Output JSON, replacing rows left by an earlier attempt.",
      }),
    );

    const finalizeVersion = this.invokeLambda(
      "FinalizeEmbeddingVersion",
      props.embeddingsFunction,
      {
        comment:
          "Publish the version as READY; an empty table fails the run.",
        payload: {
          action: "finalize",
          requestKey: jsonata(REQUEST_KEY),
          tableName: jsonata("$embeddingsTable"),
        },
      },
    );

    const { containerName } = props.reviewContainer;
    const reviewRequirements = tasks.EcsRunTask.jsonata(
      this,
      "ReviewRequirements",
      {
        cluster: props.cluster,
        comment:
          "Review every legal requirement against this run's table and write the results to S3.",
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
    reviewExitedWithError.next(markVersionFailed);

    const notifyCompleted = this.notify(
      "NotifyCompleted",
      "COMPLETED",
      [
        `"requirementCount": ${RUN_INPUT}.requirementCount`,
        `"embeddingsTable": $embeddingsTable`,
        `"embeddingVersion": $embeddingVersion`,
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
      allocateVersion,
      embedDocuments,
      finalizeVersion,
      reviewRequirements,
    ]) {
      step.addCatch(markVersionFailed, {
        assign: { failure: jsonata("$states.errorOutput") },
        errors: [sfn.Errors.ALL],
      });
    }

    const definition = preProcessDocuments
      .next(allocateVersion)
      .next(embedDocuments)
      .next(finalizeVersion)
      .next(reviewRequirements)
      .next(reviewSucceeded);

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
