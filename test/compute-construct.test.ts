import * as cdk from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { describe, expect, it } from "vitest";

import { AisuiteInfrastructureStack } from "../src/aisuite-infrastructure-stack";
import {
  API_CONTAINER_PORT,
  API_HEALTH_CHECK_PATH,
} from "../src/constructs/compute-construct";
import { STUB_VPC_CONTEXT_KEY } from "../src/constructs/networking-construct";
import {
  type DeploymentEnvironmentName,
  getDeploymentConfig,
} from "../src/deployment-config";

const EXPECTED_OUTPUTS = [
  "AlbDnsName",
  "ApiClusterName",
  "ApiServiceName",
  "ApiEcrRepositoryUri",
] as const;
const REMOVED_PIPELINE_CODE_ENVIRONMENT_NAME = [
  "PIPELINE",
  "CODE",
  "BUCKET",
].join("_");

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

function apiContainerEnvironment(
  template: Template,
  family: string,
): Record<string, string> {
  const bucketNames = Object.fromEntries(
    Object.entries(template.findResources("AWS::S3::Bucket")).map(
      ([logicalId, bucket]) => [
        logicalId,
        bucket.Properties?.BucketName as string,
      ],
    ),
  );

  const containers = Object.values(
    template.findResources("AWS::ECS::TaskDefinition", {
      Properties: { Family: family },
    }),
  ).flatMap(
    (taskDefinition) =>
      (taskDefinition.Properties?.ContainerDefinitions as Array<{
        Environment?: Array<{ Name: string; Value: unknown }>;
      }>) ?? [],
  );

  return Object.fromEntries(
    containers.flatMap(
      (container) =>
        container.Environment?.map(({ Name, Value }) => [
          Name,
          typeof Value === "string"
            ? Value
            : (bucketNames[(Value as { Ref?: string }).Ref ?? ""] ??
              JSON.stringify(Value)),
        ]) ?? [],
    ),
  );
}

describe.each(["dev", "prod"] as const)(
  "AISuite %s RAG API",
  (environmentName) => {
    const serviceName = `aisuite-${environmentName}-rag-api`;

    it("runs the API on a Fargate service in its own cluster", () => {
      const template = synthesize(environmentName);

      template.hasResourceProperties("AWS::ECS::Cluster", {
        ClusterName: serviceName,
      });
      template.hasResourceProperties("AWS::ECR::Repository", {
        RepositoryName: serviceName,
      });
      template.hasResourceProperties("AWS::ECS::TaskDefinition", {
        Family: serviceName,
        NetworkMode: "awsvpc",
        RequiresCompatibilities: ["FARGATE"],
        ContainerDefinitions: Match.arrayWith([
          Match.objectLike({
            PortMappings: [
              { ContainerPort: API_CONTAINER_PORT, Protocol: "tcp" },
            ],
          }),
        ]),
      });
      template.hasResourceProperties("AWS::ECS::Service", {
        LaunchType: "FARGATE",
        ServiceName: serviceName,
        NetworkConfiguration: {
          AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: "DISABLED" }),
        },
      });
    });

    it("fronts the service with an internal ALB targeting the health endpoint", () => {
      const template = synthesize(environmentName);

      template.hasResourceProperties(
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        { Scheme: "internal", Type: "application" },
      );
      template.hasResourceProperties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {
          HealthCheckPath: API_HEALTH_CHECK_PATH,
          Port: API_CONTAINER_PORT,
          Protocol: "HTTP",
          TargetType: "ip",
        },
      );
    });

    it("keeps every ingress rule off the public internet", () => {
      const template = synthesize(environmentName);

      const inlineRules = Object.values(
        template.findResources("AWS::EC2::SecurityGroup"),
      ).flatMap(
        (group) =>
          (group.Properties?.SecurityGroupIngress as Array<
            Record<string, unknown>
          >) ?? [],
      );
      const standaloneRules = Object.values(
        template.findResources("AWS::EC2::SecurityGroupIngress"),
      ).map((rule) => rule.Properties as Record<string, unknown>);

      expect(inlineRules.length).toBeGreaterThan(0);
      for (const rule of [...inlineRules, ...standaloneRules]) {
        expect(rule).not.toHaveProperty("CidrIp", "0.0.0.0/0");
        expect(rule).not.toHaveProperty("CidrIpv6", "::/0");
      }
    });

    it("allows ALB traffic to the application tier on the container port only", () => {
      const template = synthesize(environmentName);

      template.hasResourceProperties("AWS::EC2::SecurityGroupIngress", {
        FromPort: API_CONTAINER_PORT,
        IpProtocol: "tcp",
        ToPort: API_CONTAINER_PORT,
      });
      template.hasResourceProperties("AWS::EC2::SecurityGroupEgress", {
        FromPort: API_CONTAINER_PORT,
        IpProtocol: "tcp",
        ToPort: API_CONTAINER_PORT,
      });
    });

    it("passes the bucket, database, and Bedrock contract to the container", () => {
      const template = synthesize(environmentName);
      const environment = apiContainerEnvironment(template, serviceName);

      expect(environment.DOCUMENTS_BUCKET).toBe(
        `aisuite-${environmentName}-contract-rag`,
      );
      expect(environment.POST_PROCESSING_BUCKET).toBe(
        `aisuite-${environmentName}-contract-rag-post-processing`,
      );
      expect(environment).not.toHaveProperty(
        REMOVED_PIPELINE_CODE_ENVIRONMENT_NAME,
      );
      expect(environment.PIPELINE_TEMP_BUCKET).toBe(
        `aisuite-${environmentName}-llm-pipeline-temp`,
      );
      expect(environment.DB_SECRET_ARN).toBeDefined();
      expect(environment.BEDROCK_MODEL_ID).toBe("us.amazon.nova-pro-v1:0");
      expect(environment.BEDROCK_EMBED_MODEL_ID).toBe("us.cohere.embed-v4:0");
      expect(environment.AWS_REGION).toBe("us-east-1");
      expect(environment.AWS_DEFAULT_REGION).toBe("us-east-1");
      expect(environment.VERDICT_PERSISTENCE_ENABLED).toBe(
        environmentName === "dev" ? "true" : "false",
      );
    });

    it("exposes the compute output contract", () => {
      const template = synthesize(environmentName);

      for (const outputName of EXPECTED_OUTPUTS) {
        template.hasOutput(outputName, {});
      }
    });
  },
);
