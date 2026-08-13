import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";

import { AisuiteInfrastructureStack } from "../src/aisuite-infrastructure-stack";
import { INGESTION_ACTIVATION_CONTEXT_KEY } from "../src/constructs/ingestion-construct";
import { STUB_VPC_CONTEXT_KEY } from "../src/constructs/networking-construct";
import {
  type DeploymentEnvironmentName,
  getDeploymentConfig,
} from "../src/deployment-config";

function synthesize(
  environmentName: DeploymentEnvironmentName,
  context: Record<string, unknown> = {},
): Template {
  const app = new cdk.App({
    context: { [STUB_VPC_CONTEXT_KEY]: true, ...context },
  });
  const config = getDeploymentConfig(environmentName);
  const stack = new AisuiteInfrastructureStack(app, config.stackName, {
    deploymentConfig: config,
    env: config.awsEnvironment,
    stackName: config.stackName,
  });

  return Template.fromStack(stack);
}

function eventRuleState(template: Template): string | undefined {
  const rules = Object.values(template.findResources("AWS::Events::Rule"));
  expect(rules).toHaveLength(1);
  return rules[0]?.Properties?.State as string | undefined;
}

function stateMachineDefinition(template: Template): string {
  const machines = Object.values(
    template.findResources("AWS::StepFunctions::StateMachine"),
  );
  expect(machines).toHaveLength(1);
  return JSON.stringify(machines[0]?.Properties?.DefinitionString ?? "");
}

describe("event-driven ingestion activation gate", () => {
  it("enables the rule in dev when ingestion activation is true", () => {
    const template = synthesize("dev", {
      [INGESTION_ACTIVATION_CONTEXT_KEY]: true,
    });

    expect(eventRuleState(template)).toBe("ENABLED");
  });

  it("keeps the dev rule disabled without ingestion activation", () => {
    expect(eventRuleState(synthesize("dev"))).toBe("DISABLED");
  });

  it.each(["qa", "uat", "prod"] as const)(
    "keeps the %s rule disabled even when ingestion activation is true",
    (environmentName) => {
      const template = synthesize(environmentName, {
        [INGESTION_ACTIVATION_CONTEXT_KEY]: true,
      });

      expect(eventRuleState(template)).toBe("DISABLED");
    },
  );
});

describe("event-driven ingestion cross-environment topology", () => {
  it.each(["dev", "qa", "uat", "prod"] as const)(
    "synthesizes %s ingestion infrastructure without S3 notification Lambdas",
    (environmentName) => {
      const template = synthesize(environmentName);

      template.resourceCountIs("AWS::Events::Rule", 1);
      template.resourceCountIs("Custom::S3BucketNotifications", 0);
      template.hasResourceProperties("AWS::StepFunctions::StateMachine", {
        StateMachineName: `aisuite-${environmentName}-rag-ingestion`,
      });
      template.hasResourceProperties("AWS::SNS::Topic", {
        TopicName: `aisuite-${environmentName}-rag-ingestion-alerts`,
      });

      const notificationLambdas = Object.keys(
        template.findResources("AWS::Lambda::Function"),
      ).filter((logicalId) => logicalId.includes("BucketNotifications"));
      expect(notificationLambdas).toHaveLength(0);
    },
  );
});

describe("event-driven ingestion state machine", () => {
  it("chains debounce, single-flight, RUN_JOB tasks, and SNS catch", () => {
    const template = synthesize("dev");
    const definition = stateMachineDefinition(template);

    expect(definition).toContain("ecs:runTask.sync");
    expect(definition).toContain("aws-sdk:sfn:listExecutions");
    expect(definition).toContain("OldestRunningExecutionOnly");
    expect(definition).toContain("SkipDuplicateBurst");
    expect(definition).toContain("sns:publish");
    expect(definition).toContain("EMBEDDINGS_TABLE_NAME");
  });

  it("alarms failed and timed-out executions onto the ingestion topic", () => {
    const template = synthesize("dev");
    const alarms = Object.values(
      template.findResources("AWS::CloudWatch::Alarm"),
    );
    const ingestionAlarms = alarms.filter((alarm) => {
      const name = alarm.Properties?.AlarmName as string | undefined;
      return name?.startsWith("aisuite-dev-rag-ingestion-");
    });

    expect(ingestionAlarms).toHaveLength(2);
    for (const alarm of ingestionAlarms) {
      expect(alarm.Properties?.AlarmActions).toHaveLength(1);
    }
  });
});
