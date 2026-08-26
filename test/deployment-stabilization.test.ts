import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";

import { AisuiteInfrastructureStack } from "../src/aisuite-infrastructure-stack";
import { STUB_VPC_CONTEXT_KEY } from "../src/constructs/networking-construct";
import {
  type DeploymentEnvironmentName,
  getDeploymentConfig,
} from "../src/deployment-config";

const ACTIVATE_API_CONTEXT_KEY = "aisuite:activateApi";
const API_IMAGE_TAG_CONTEXT_KEY = "aisuite:apiImageTag";
const BATCH_IMAGE_TAG_CONTEXT_KEY = "aisuite:batchImageTag";
const IMMUTABLE_IMAGE_TAG = "0123456789abcdef0123456789abcdef01234567";

interface CloudFormationResource {
  DeletionPolicy?: string;
  Properties?: Record<string, unknown>;
  Type?: string;
  UpdateReplacePolicy?: string;
}

interface TaskContainer {
  Environment?: Array<{ Name: string; Value: unknown }>;
  Image?: unknown;
  Secrets?: Array<{ Name: string; ValueFrom: unknown }>;
}

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

function taskDefinitionByFamily(
  template: Template,
  family: string,
): CloudFormationResource {
  const matches = Object.values(
    template.findResources("AWS::ECS::TaskDefinition", {
      Properties: { Family: family },
    }),
  ) as CloudFormationResource[];

  expect(matches).toHaveLength(1);
  const taskDefinition = matches[0];
  expect(taskDefinition).toBeDefined();
  return taskDefinition ?? {};
}

function containersOf(resource: CloudFormationResource): TaskContainer[] {
  return (
    (resource.Properties?.ContainerDefinitions as TaskContainer[] | undefined) ??
    []
  );
}

describe("two-phase API activation", () => {
  it.each([
    ["absent", {}],
    ["false", { [ACTIVATE_API_CONTEXT_KEY]: false }],
  ])("keeps DesiredCount at zero when activation is %s", (_label, context) => {
    const template = synthesize("dev", context);

    template.hasResourceProperties("AWS::ECS::Service", {
      DesiredCount: 0,
      ServiceName: "aisuite-dev-rag-api",
    });
  });

  it.each([
    ["dev", 1],
    ["qa", 2],
    ["uat", 2],
    ["prod", 2],
  ] as const)(
    "activates the %s API at its environment count",
    (environmentName, expectedCount) => {
      const template = synthesize(environmentName, {
        [ACTIVATE_API_CONTEXT_KEY]: true,
      });

      template.hasResourceProperties("AWS::ECS::Service", {
        DesiredCount: expectedCount,
        ServiceName: `aisuite-${environmentName}-rag-api`,
      });
    },
  );
});

describe("immutable x86-64 task images", () => {
  it("sets Linux x86-64 on the API and both batch task definitions", () => {
    const template = synthesize("dev");

    for (const family of [
      "aisuite-dev-rag-api",
      "aisuite-dev-pre-processing",
      "aisuite-dev-rag-process",
    ]) {
      expect(taskDefinitionByFamily(template, family).Properties).toEqual(
        expect.objectContaining({
          RuntimePlatform: {
            CpuArchitecture: "X86_64",
            OperatingSystemFamily: "LINUX",
          },
        }),
      );
    }
  });

  it("uses the immutable API and batch image-tag contexts", () => {
    const template = synthesize("dev", {
      [API_IMAGE_TAG_CONTEXT_KEY]: IMMUTABLE_IMAGE_TAG,
      [BATCH_IMAGE_TAG_CONTEXT_KEY]: IMMUTABLE_IMAGE_TAG,
    });

    for (const family of [
      "aisuite-dev-rag-api",
      "aisuite-dev-pre-processing",
      "aisuite-dev-rag-process",
    ]) {
      const [container] = containersOf(taskDefinitionByFamily(template, family));
      expect(JSON.stringify(container?.Image)).toContain(IMMUTABLE_IMAGE_TAG);
      expect(JSON.stringify(container?.Image)).not.toContain("latest");
    }
  });
});

describe("environment-appropriate ECR removal", () => {
  it("destroys and empties both dev repositories on stack deletion", () => {
    const resources = Object.values(
      synthesize("dev").toJSON().Resources as Record<
        string,
        CloudFormationResource
      >,
    ).filter((resource) => resource.Type === "AWS::ECR::Repository");

    expect(resources).toHaveLength(2);
    for (const resource of resources) {
      expect(resource.DeletionPolicy).toBe("Delete");
      expect(resource.UpdateReplacePolicy).toBe("Delete");
      expect(resource.Properties?.EmptyOnDelete).toBe(true);
    }
  });

  it.each(["qa", "uat", "prod"] as const)(
    "retains and does not empty both %s repositories",
    (environmentName) => {
      const resources = Object.values(
        synthesize(environmentName).toJSON().Resources as Record<
          string,
          CloudFormationResource
        >,
      ).filter((resource) => resource.Type === "AWS::ECR::Repository");

      expect(resources).toHaveLength(2);
      for (const resource of resources) {
        expect(resource.DeletionPolicy).toBe("Retain");
        expect(resource.UpdateReplacePolicy).toBe("Retain");
        expect(resource.Properties?.EmptyOnDelete).not.toBe(true);
      }
    },
  );
});

describe("database bootstrap task contract", () => {
  it("creates a Linux x86-64 bootstrap task with secret injection only", () => {
    const template = synthesize("dev");
    const bootstrapTasks = Object.values(
      template.findResources("AWS::ECS::TaskDefinition", {
        Properties: {
          Family: Match.stringLikeRegexp("bootstrap"),
        },
      }),
    ) as CloudFormationResource[];

    expect(bootstrapTasks).toHaveLength(1);
    const bootstrapTask = bootstrapTasks[0];
    expect(bootstrapTask?.Properties).toEqual(
      expect.objectContaining({
        NetworkMode: "awsvpc",
        RequiresCompatibilities: ["FARGATE"],
        RuntimePlatform: {
          CpuArchitecture: "X86_64",
          OperatingSystemFamily: "LINUX",
        },
      }),
    );

    const [container] = containersOf(bootstrapTask ?? {});
    const secretNames = container?.Secrets?.map(({ Name }) => Name) ?? [];
    expect(secretNames).toEqual(
      expect.arrayContaining([
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "PGDATABASE",
        "PGPASSWORD",
        "APP_PASSWORD",
      ]),
    );
    expect(container?.Secrets).toHaveLength(6);

    const plaintextNames =
      container?.Environment?.map(({ Name }) => Name).filter((name) =>
        /PASS(?:WORD)?|SECRET/i.test(name),
      ) ?? [];
    expect(plaintextNames).toEqual([]);
  });

  it("restricts bootstrap secret reads to the master and app secret ARNs", () => {
    const template = synthesize("dev");
    const bootstrapTasks = Object.values(
      template.findResources("AWS::ECS::TaskDefinition", {
        Properties: {
          Family: Match.stringLikeRegexp("bootstrap"),
        },
      }),
    ) as CloudFormationResource[];
    const secrets = template.findResources("AWS::SecretsManager::Secret");
    const masterSecretId = Object.entries(secrets).find(([, resource]) =>
      JSON.stringify(resource.Properties?.Name).includes(
        "/rag-db-credentials",
      ),
    )?.[0];
    const appSecretId = Object.entries(secrets).find(([, resource]) =>
      JSON.stringify(resource.Properties?.Name).includes(
        "/rag-app-db-credentials",
      ),
    )?.[0];
    expect(bootstrapTasks).toHaveLength(1);
    const executionRoleArn = bootstrapTasks[0]?.Properties?.ExecutionRoleArn as
      | { "Fn::GetAtt"?: [string, string] }
      | undefined;
    const executionRoleId = executionRoleArn?.["Fn::GetAtt"]?.[0];
    expect(executionRoleId).toBeDefined();

    const bootstrapPolicies = Object.values(
      template.findResources("AWS::IAM::Policy"),
    ).filter((policy) =>
      JSON.stringify(policy.Properties?.Roles).includes(executionRoleId ?? ""),
    );
    const statements = bootstrapPolicies.flatMap(
      (policy) =>
        (policy.Properties?.PolicyDocument?.Statement as
          | Array<{ Action?: string | string[]; Resource?: unknown }>
          | undefined) ?? [],
    );
    const getSecretStatements = statements.filter((statement) => {
      const actions = Array.isArray(statement.Action)
        ? statement.Action
        : [statement.Action];
      return actions.includes("secretsmanager:GetSecretValue");
    });
    const serializedResources = JSON.stringify(
      getSecretStatements.map(({ Resource }) => Resource),
    );

    expect(masterSecretId).toBeDefined();
    expect(appSecretId).toBeDefined();
    expect(getSecretStatements).toHaveLength(1);
    expect(serializedResources).toContain(masterSecretId);
    expect(serializedResources).toContain(appSecretId);
    expect(serializedResources).not.toContain('"*"');
  });

  it("outputs the bootstrap RunTask network contract", () => {
    const template = synthesize("dev");

    for (const outputName of [
      "BootstrapClusterName",
      "BootstrapTaskDefinitionArn",
      "BootstrapSecurityGroupId",
      "BootstrapSubnetIds",
    ]) {
      template.hasOutput(outputName, {});
    }
  });
});

describe("deployment workflow contract", () => {
  const workflowPath = fileURLToPath(
    new URL("../.github/workflows/deploy.yml", import.meta.url),
  );
  const workflow = readFileSync(workflowPath, "utf8");

  it("orders infrastructure, images, bootstrap, activation, and health checks", () => {
    const orderedMarkerAlternatives = [
      [`--context ${ACTIVATE_API_CONTEXT_KEY}=false`],
      ["amazon-ecr-login", "aws ecr get-login-password"],
      ["docker buildx build"],
      ["aws ecs run-task"],
      ["aws ecs wait tasks-stopped"],
      ["exitCode"],
      [`--context ${ACTIVATE_API_CONTEXT_KEY}=true`],
      ["aws ecs wait services-stable"],
      ["aws elbv2 describe-target-health"],
    ];
    const positions = orderedMarkerAlternatives.map((alternatives) =>
      Math.max(...alternatives.map((marker) => workflow.indexOf(marker))),
    );

    expect(positions.every((position) => position >= 0)).toBe(true);
    expect(positions).toEqual([...positions].sort((left, right) => left - right));
  });

  it("builds and pushes API and batch SHA images for linux/amd64", () => {
    expect(workflow).toContain("--platform linux/amd64");
    expect(workflow).toContain("--push");
    expect(workflow).toMatch(
      /\$\{\{\s*github\.sha\s*\}\}|\$\{GITHUB_SHA\}|\$GITHUB_SHA/,
    );
    expect(workflow).toContain("ApiEcrRepositoryUri");
    expect(workflow).toContain("BatchEcrRepositoryUri");
    expect(workflow).toMatch(
      new RegExp(
        `--context ${API_IMAGE_TAG_CONTEXT_KEY.replace(":", "\\:")}=(?:\\$\\{\\{\\s*github\\.sha\\s*\\}\\}|\\$\\{GITHUB_SHA\\}|\\$GITHUB_SHA)`,
      ),
    );
    expect(workflow).toMatch(
      new RegExp(
        `--context ${BATCH_IMAGE_TAG_CONTEXT_KEY.replace(":", "\\:")}=(?:\\$\\{\\{\\s*github\\.sha\\s*\\}\\}|\\$\\{GITHUB_SHA\\}|\\$GITHUB_SHA)`,
      ),
    );
  });

  it("never deploys latest or passes database secrets through arguments", () => {
    expect(workflow).not.toMatch(/(?:^|[:=\s])latest(?:$|[\s"'])/m);
    expect(workflow).not.toContain("bootstrap_dev_db.py");
    expect(workflow).not.toContain("secretsmanager get-secret-value");
    expect(workflow).not.toMatch(/(?:PGPASSWORD|APP_PASS(?:WORD)?)=/);
    expect(workflow).not.toMatch(/--(?:password|secret)(?:\s|=)/i);
  });

  it("sets DEPLOYMENT_TIMESTAMP for push and workflow_dispatch", () => {
    expect(workflow).toContain(
      "DEPLOYMENT_TIMESTAMP: ${{ github.event_name == 'push' && github.event.head_commit.timestamp || github.run_started_at }}",
    );
  });

  it("prints bootstrap CloudWatch logs on task failure", () => {
    expect(workflow).toContain("aws logs get-log-events");
    expect(workflow).toContain("bootstrap/Container/");
  });
});
