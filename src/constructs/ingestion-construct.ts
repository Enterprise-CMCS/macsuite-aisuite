import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cloudwatchActions from "aws-cdk-lib/aws-cloudwatch-actions";
import type * as ecs from "aws-cdk-lib/aws-ecs";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import type * as s3 from "aws-cdk-lib/aws-s3";
import * as sns from "aws-cdk-lib/aws-sns";
import * as sfn from "aws-cdk-lib/aws-stepfunctions";
import * as tasks from "aws-cdk-lib/aws-stepfunctions-tasks";
import { Construct } from "constructs";

import { type ActiveContract, resolveActiveContract } from "../contract-config";
import { APPLICATION_NAME, type DeploymentConfig } from "../deployment-config";
import type { BatchRunTaskNetworkConfiguration } from "./batch-construct";

export const INGESTION_ACTIVATION_CONTEXT_KEY = "aisuite:activateIngestion";

export const INGESTION_DEBOUNCE_MINUTES_CONTEXT_KEY =
  "aisuite:ingestionDebounceMinutes";

export const DEFAULT_INGESTION_DEBOUNCE_MINUTES = 5;

const RUN_TASK_TIMEOUT = cdk.Duration.hours(3);
const STATE_MACHINE_TIMEOUT = cdk.Duration.hours(8);
const RUNNING_EXECUTIONS_PATH = "$.runningExecutions.Executions";

export interface IngestionConstructProps {
  cluster: ecs.ICluster;
  deploymentConfig: DeploymentConfig;
  documentsBucket: s3.Bucket;
  preProcessingTaskDefinition: ecs.TaskDefinition;
  ragProcessTaskDefinition: ecs.TaskDefinition;
  runTaskNetworkConfiguration: BatchRunTaskNetworkConfiguration;
}

export class IngestionConstruct extends Construct {
  public readonly activeContract: ActiveContract;
  public readonly alertTopic: sns.Topic;
  public readonly rule: events.Rule;
  public readonly stateMachine: sfn.StateMachine;

  public constructor(
    scope: Construct,
    id: string,
    props: IngestionConstructProps,
  ) {
    super(scope, id);

    const { name } = props.deploymentConfig;
    const stateMachineName = `${APPLICATION_NAME}-${name}-rag-ingestion`;

    this.activeContract = resolveActiveContract();

    const cfnBucket = props.documentsBucket.node.defaultChild as s3.CfnBucket;
    cfnBucket.notificationConfiguration = {
      eventBridgeConfiguration: { eventBridgeEnabled: true },
    };

    this.alertTopic = new sns.Topic(this, "AlertTopic", {
      displayName: `AISuite ${name} RAG ingestion alerts`,
      topicName: `${stateMachineName}-alerts`,
    });

    const failure = new sfn.Fail(this, "IngestionFailed", {
      cause: "AISuite RAG ingestion failed; see the ingestion alert topic.",
      error: "IngestionFailed",
    });

    const preProcessing = this.createRunTask({
      cluster: props.cluster,
      embeddingsTableName: this.activeContract.embeddingsTableName,
      id: "PreProcessing",
      network: props.runTaskNetworkConfiguration,
      taskDefinition: props.preProcessingTaskDefinition,
    });
    preProcessing.addCatch(
      this.createFailureHandler({
        environmentName: name,
        failedState: "PreProcessing",
        failure,
        id: "PublishPreProcessingFailure",
      }),
      { resultPath: "$.error" },
    );

    const ragProcess = this.createRunTask({
      cluster: props.cluster,
      embeddingsTableName: this.activeContract.embeddingsTableName,
      id: "RagProcess",
      network: props.runTaskNetworkConfiguration,
      taskDefinition: props.ragProcessTaskDefinition,
    });
    ragProcess.addCatch(
      this.createFailureHandler({
        environmentName: name,
        failedState: "RagProcess",
        failure,
        id: "PublishRagProcessFailure",
      }),
      { resultPath: "$.error" },
    );

    const stateMachineArn = cdk.Stack.of(this).formatArn({
      arnFormat: cdk.ArnFormat.COLON_RESOURCE_NAME,
      resource: "stateMachine",
      resourceName: stateMachineName,
      service: "states",
    });

    const runningExecutions = sfn.JsonPath.objectAt(RUNNING_EXECUTIONS_PATH);
    const oldestExecutionIndex = sfn.JsonPath.mathAdd(
      sfn.JsonPath.arrayLength(runningExecutions) as unknown as number,
      -1,
    ) as unknown as number;

    const definition = new sfn.Wait(this, "DebounceUploadBurst", {
      time: sfn.WaitTime.duration(cdk.Duration.minutes(this.debounceMinutes())),
    })
      .next(
        new tasks.CallAwsService(this, "ListRunningExecutions", {
          action: "listExecutions",
          iamAction: "states:ListExecutions",
          iamResources: [stateMachineArn],
          parameters: {
            StateMachineArn: stateMachineArn,
            StatusFilter: "RUNNING",
          },
          resultPath: "$.runningExecutions",
          service: "sfn",
        }),
      )
      .next(
        new sfn.Pass(this, "ResolveOldestExecution", {
          parameters: {
            contractId: sfn.JsonPath.stringAt("$.contractId"),
            oldestExecution: sfn.JsonPath.arrayGetItem(
              runningExecutions,
              oldestExecutionIndex,
            ),
            runningExecutionCount: sfn.JsonPath.arrayLength(runningExecutions),
          },
        }),
      )
      .next(
        new sfn.Choice(this, "OldestRunningExecutionOnly")
          .when(
            sfn.Condition.or(
              sfn.Condition.numberEquals("$.runningExecutionCount", 1),
              sfn.Condition.stringEqualsJsonPath(
                "$.oldestExecution.ExecutionArn",
                "$$.Execution.Id",
              ),
            ),
            preProcessing
              .next(ragProcess)
              .next(new sfn.Succeed(this, "IngestionComplete")),
          )
          .otherwise(new sfn.Succeed(this, "SkipDuplicateBurst")),
      );

    this.stateMachine = new sfn.StateMachine(this, "StateMachine", {
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      stateMachineName,
      stateMachineType: sfn.StateMachineType.STANDARD,
      timeout: STATE_MACHINE_TIMEOUT,
    });

    this.rule = new events.Rule(this, "ObjectCreatedRule", {
      description:
        "Starts AISuite RAG ingestion when a contract document lands in the active contract prefix.",
      enabled: name === "dev" && this.ingestionActivated(),
      eventPattern: {
        detail: {
          bucket: { name: [props.documentsBucket.bucketName] },
          object: { key: [{ prefix: this.activeContract.inputPrefix }] },
        },
        detailType: ["Object Created"],
        source: ["aws.s3"],
      },
      ruleName: `${stateMachineName}-object-created`,
      targets: [
        new targets.SfnStateMachine(this.stateMachine, {
          input: events.RuleTargetInput.fromObject({
            bucketName: events.EventField.fromPath("$.detail.bucket.name"),
            contractId: this.activeContract.id,
            objectKey: events.EventField.fromPath("$.detail.object.key"),
          }),
        }),
      ],
    });

    const alarmAction = new cloudwatchActions.SnsAction(this.alertTopic);
    for (const alarm of [
      new cloudwatch.Alarm(this, "ExecutionsFailedAlarm", {
        alarmDescription: "AISuite RAG ingestion executions failed.",
        alarmName: `${stateMachineName}-failed`,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluationPeriods: 1,
        metric: this.stateMachine.metricFailed(),
        threshold: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      }),
      new cloudwatch.Alarm(this, "ExecutionsTimedOutAlarm", {
        alarmDescription: "AISuite RAG ingestion executions timed out.",
        alarmName: `${stateMachineName}-timed-out`,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluationPeriods: 1,
        metric: this.stateMachine.metricTimedOut(),
        threshold: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      }),
    ]) {
      alarm.addAlarmAction(alarmAction);
    }
  }

  private createFailureHandler(props: {
    environmentName: string;
    failedState: string;
    failure: sfn.Fail;
    id: string;
  }): sfn.IChainable {
    return new tasks.SnsPublish(this, props.id, {
      message: sfn.TaskInput.fromObject({
        cause: sfn.JsonPath.stringAt("$.error.Cause"),
        contractId: sfn.JsonPath.stringAt("$.contractId"),
        environment: props.environmentName,
        error: sfn.JsonPath.stringAt("$.error.Error"),
        executionId: sfn.JsonPath.stringAt("$$.Execution.Id"),
        failedState: props.failedState,
      }),
      resultPath: sfn.JsonPath.DISCARD,
      subject: `AISuite ${props.environmentName} RAG ingestion failure`,
      topic: this.alertTopic,
    }).next(props.failure);
  }

  private createRunTask(props: {
    cluster: ecs.ICluster;
    embeddingsTableName: string;
    id: string;
    network: BatchRunTaskNetworkConfiguration;
    taskDefinition: ecs.TaskDefinition;
  }): tasks.EcsRunTask {
    return new tasks.EcsRunTask(this, props.id, {
      assignPublicIp: props.network.assignPublicIp,
      cluster: props.cluster,
      containerOverrides: [
        {
          containerDefinition: props.taskDefinition.defaultContainer!,
          environment: [
            {
              name: "EMBEDDINGS_TABLE_NAME",
              value: props.embeddingsTableName,
            },
          ],
        },
      ],
      integrationPattern: sfn.IntegrationPattern.RUN_JOB,
      launchTarget: new tasks.EcsFargateLaunchTarget(),
      resultPath: sfn.JsonPath.DISCARD,
      securityGroups: props.network.securityGroups,
      subnets: props.network.subnetSelection,
      taskDefinition: props.taskDefinition,
      taskTimeout: sfn.Timeout.duration(RUN_TASK_TIMEOUT),
    });
  }

  private debounceMinutes(): number {
    const contextValue = this.node.tryGetContext(
      INGESTION_DEBOUNCE_MINUTES_CONTEXT_KEY,
    ) as unknown;
    const minutes =
      typeof contextValue === "number"
        ? contextValue
        : typeof contextValue === "string"
          ? Number.parseInt(contextValue, 10)
          : Number.NaN;

    return Number.isFinite(minutes) && minutes > 0
      ? minutes
      : DEFAULT_INGESTION_DEBOUNCE_MINUTES;
  }

  private ingestionActivated(): boolean {
    const contextValue = this.node.tryGetContext(
      INGESTION_ACTIVATION_CONTEXT_KEY,
    ) as unknown;

    return contextValue === true || contextValue === "true";
  }
}
