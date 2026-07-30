import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";

import { AisuiteInfrastructureStack } from "../src/aisuite-infrastructure-stack";
import { STUB_VPC_CONTEXT_KEY } from "../src/constructs/networking-construct";
import {
  DEPLOYMENT_ENVIRONMENT_NAMES,
  type DeploymentEnvironmentName,
  getDeploymentConfig,
} from "../src/deployment-config";

const EXPECTED_OUTPUTS = [
  "DeploymentEnvironment",
  "ProtectedEnvironment",
  "DbEndpoint",
  "DbSecretArn",
  "AppDbSecretArn",
  "AppTaskRoleArn",
  "DocumentsBucketName",
  "PostProcessingBucketName",
  "PipelineCodeBucketName",
  "PipelineTempBucketName",
  "AlbDnsName",
  "ApiClusterName",
  "ApiServiceName",
  "ApiEcrRepositoryUri",
  "BatchClusterName",
  "PreProcessingTaskDefinitionArn",
  "PreProcessingTaskDefinitionFamily",
  "RagProcessTaskDefinitionArn",
  "RagProcessTaskDefinitionFamily",
  "BatchEcrRepositoryUri",
] as const;

const DATA_RESOURCES = new Set([
  "AWS::RDS::DBInstance",
  "AWS::S3::Bucket",
  "AWS::SecretsManager::Secret",
]);

const PROTECTED_ENVIRONMENT_NAMES = ["qa", "uat", "prod"] as const satisfies
  readonly DeploymentEnvironmentName[];

function synthesize(environmentName: DeploymentEnvironmentName): Template {
  const app = new cdk.App({ context: { [STUB_VPC_CONTEXT_KEY]: true } });
  const config = getDeploymentConfig(environmentName);
  const stack = new AisuiteInfrastructureStack(app, config.stackName, {
    deploymentConfig: config,
    env: config.awsEnvironment,
    stackName: config.stackName,
  });

  return Template.fromStack(stack);
}

describe.each(DEPLOYMENT_ENVIRONMENT_NAMES)(
  "AISuite %s data plane",
  (environmentName) => {
    it("creates exactly four private S3 buckets", () => {
      const template = synthesize(environmentName);

      template.resourceCountIs("AWS::S3::Bucket", 4);
      template.allResourcesProperties("AWS::S3::Bucket", {
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
      });
    });

    it("limits database ingress to security-group sources", () => {
      const template = synthesize(environmentName);
      const dbSecurityGroups = template.findResources(
        "AWS::EC2::SecurityGroup",
        {
          Properties: {
            GroupDescription: Match.stringLikeRegexp(
              "AISuite RAG PostgreSQL",
            ),
          },
        },
      );

      expect(Object.keys(dbSecurityGroups)).toHaveLength(1);

      for (const resource of Object.values(
        template.findResources("AWS::EC2::SecurityGroupIngress"),
      )) {
        expect(resource.Properties).not.toHaveProperty("CidrIp", "0.0.0.0/0");
      }
    });

    it("encrypts RDS storage and provisions master + app database secrets", () => {
      const template = synthesize(environmentName);

      template.hasResourceProperties("AWS::RDS::DBInstance", {
        StorageEncrypted: true,
      });
      template.resourceCountIs("AWS::SecretsManager::Secret", 2);
      template.hasResourceProperties("AWS::SecretsManager::Secret", {
        Name: Match.stringLikeRegexp("/rag-db-credentials$"),
        GenerateSecretString: {
          SecretStringTemplate: Match.serializedJson({
            username: "aisuite_admin",
          }),
        },
      });
      template.hasResourceProperties("AWS::SecretsManager::Secret", {
        Name: Match.stringLikeRegexp("/rag-app-db-credentials$"),
        GenerateSecretString: {
          SecretStringTemplate: {
            "Fn::Join": Match.arrayWith([
              Match.arrayWith([Match.stringLikeRegexp("aisuite_app")]),
            ]),
          },
        },
      });
    });

    it("schedules multi-user rotation for the app database secret", () => {
      const template = synthesize(environmentName);

      template.resourceCountIs("AWS::SecretsManager::RotationSchedule", 1);
      template.hasResourceProperties("AWS::SecretsManager::RotationSchedule", {
        RotationRules: {
          ScheduleExpression: "rate(30 days)",
        },
      });
    });

    it("scopes the app task role to app secret and least-privilege S3/Bedrock", () => {
      const template = synthesize(environmentName);
      const policies = Object.values(
        template.findResources("AWS::IAM::Policy"),
      );
      const statements = policies.flatMap(
        (policy) =>
          (policy.Properties?.PolicyDocument?.Statement as Array<{
            Action?: string | string[];
            Resource?: unknown;
            Sid?: string;
          }>) ?? [],
      );

      const bySid = Object.fromEntries(
        statements
          .filter((statement) => statement.Sid)
          .map((statement) => [statement.Sid, statement]),
      );

      expect(bySid.RagAppDatabaseCredentials).toBeDefined();
      expect(bySid.RagDatabaseCredentials).toBeUndefined();
      expect(bySid.DocumentsBucketList?.Action).toBe("s3:ListBucket");
      expect(bySid.DocumentsBucketRead?.Action).toBe("s3:GetObject");
      expect(bySid.PostProcessingBucketList?.Action).toBe("s3:ListBucket");
      expect(bySid.PostProcessingBucketAccess?.Action).toEqual(
        expect.arrayContaining([
          "s3:DeleteObject",
          "s3:GetObject",
          "s3:PutObject",
        ]),
      );
      expect(bySid.PipelineTempBucketList?.Action).toBe("s3:ListBucket");
      expect(bySid.PipelineTempBucketAccess?.Action).toEqual(
        expect.arrayContaining(["s3:GetObject", "s3:PutObject"]),
      );
      expect(bySid.PipelineTempBucketAccess?.Action).not.toEqual(
        expect.arrayContaining(["s3:DeleteObject"]),
      );

      const serialized = JSON.stringify(statements);
      expect(serialized).not.toContain("llm-pipeline-code");
      expect(serialized).not.toContain("foundation-model/*");
      expect(serialized).not.toContain("inference-profile/*");
      expect(serialized).toContain("amazon.nova-pro-v1:0");
      expect(serialized).toContain("cohere.embed-v4:0");
      expect(serialized).toContain("us.amazon.nova-pro-v1:0");
      expect(serialized).toContain("us.cohere.embed-v4:0");
      expect(serialized).toContain("cohere.rerank-v3-5:0");
      expect(bySid.BedrockRerank?.Action).toEqual("bedrock:Rerank");
      expect(bySid.BedrockDataAutomationRuntime).toBeDefined();
    });

    it("preserves the stack output contract", () => {
      const template = synthesize(environmentName);

      for (const outputName of EXPECTED_OUTPUTS) {
        template.hasOutput(outputName, {});
      }
    });
  },
);

describe("environment-specific data protection", () => {
  it("allows dev teardown", () => {
    const template = synthesize("dev");

    template.hasResourceProperties("AWS::RDS::DBInstance", {
      DeletionProtection: false,
    });

    const resources = Object.values(
      template.toJSON().Resources as Record<
        string,
        {
          DeletionPolicy?: string;
          Type?: string;
          UpdateReplacePolicy?: string;
        }
      >,
    ).filter(
      (resource) =>
        DATA_RESOURCES.has(resource.Type ?? "") &&
        (resource.DeletionPolicy === "Retain" ||
          resource.UpdateReplacePolicy === "Retain"),
    );

    expect(resources).toHaveLength(0);
  });

  it.each(PROTECTED_ENVIRONMENT_NAMES)(
    "protects %s resources from deletion",
    (environmentName) => {
      const template = synthesize(environmentName);

      template.hasResourceProperties("AWS::RDS::DBInstance", {
        DeletionProtection: true,
      });

      const resources = Object.values(
        template.toJSON().Resources as Record<
          string,
          {
            DeletionPolicy?: string;
            Type?: string;
            UpdateReplacePolicy?: string;
          }
        >,
      ).filter(
        (resource) =>
          DATA_RESOURCES.has(resource.Type ?? "") &&
          resource.DeletionPolicy === "Retain" &&
          resource.UpdateReplacePolicy === "Retain",
      );

      expect(resources).toHaveLength(7);
    },
  );
});
