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
  type DeploymentConfig,
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

const TEST_CERTIFICATE_ARN =
  "arn:aws:acm:us-east-1:123456789012:certificate/test-id";

function synthesizeWithConfig(config: DeploymentConfig): Template {
  const app = new cdk.App({ context: { [STUB_VPC_CONTEXT_KEY]: true } });
  const stack = new AisuiteInfrastructureStack(app, config.stackName, {
    deploymentConfig: config,
    env: config.awsEnvironment,
    stackName: config.stackName,
  });

  return Template.fromStack(stack);
}

function synthesize(environmentName: DeploymentEnvironmentName): Template {
  return synthesizeWithConfig(getDeploymentConfig(environmentName));
}

function listeners(template: Template): Array<Record<string, unknown>> {
  return Object.values(
    template.findResources("AWS::ElasticLoadBalancingV2::Listener"),
  ).map((listener) => listener.Properties as Record<string, unknown>);
}

function ingressRules(template: Template): Array<Record<string, unknown>> {
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

  return [...inlineRules, ...standaloneRules];
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
        {
          LoadBalancerAttributes: Match.arrayWith([
            { Key: "idle_timeout.timeout_seconds", Value: "300" },
          ]),
          Scheme: "internal",
          Type: "application",
        },
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
      const rules = ingressRules(synthesize(environmentName));

      expect(rules.length).toBeGreaterThan(0);
      for (const rule of rules) {
        expect(rule).not.toHaveProperty("CidrIp", "0.0.0.0/0");
        expect(rule).not.toHaveProperty("CidrIpv6", "::/0");
      }
    });

    it("serves plain HTTP while no API certificate is configured", () => {
      const template = synthesize(environmentName);
      const albListeners = listeners(template);

      expect(albListeners).toHaveLength(1);
      expect(albListeners[0]).toMatchObject({ Port: 80, Protocol: "HTTP" });
      for (const listener of albListeners) {
        expect(listener).not.toHaveProperty("Certificates");
      }
      expect(
        albListeners.filter((listener) => listener.Protocol === "HTTPS"),
      ).toHaveLength(0);
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
      expect(environment.API_KEY_SECRET_ARN).toMatch(
        /"(?:Ref|Fn::GetAtt|Fn::Join)"/,
      );
      expect(environment.API_KEY_SECRET_ARN).not.toMatch(/^[A-Za-z0-9]{32}$/);
      expect(environment.API_ALLOWED_ORIGINS).toBe("");
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

describe("AISuite RAG API with an ALB certificate", () => {
  const template = synthesizeWithConfig({
    ...getDeploymentConfig("dev"),
    apiCertificateArn: TEST_CERTIFICATE_ARN,
  });

  it("terminates TLS on a 443 listener using TLS 1.2 or higher", () => {
    template.hasResourceProperties("AWS::ElasticLoadBalancingV2::Listener", {
      Certificates: [{ CertificateArn: TEST_CERTIFICATE_ARN }],
      Port: 443,
      Protocol: "HTTPS",
      SslPolicy: Match.stringLikeRegexp("^ELBSecurityPolicy-TLS13-1-2"),
    });
  });

  it("keeps the health-checked target group behind the HTTPS listener", () => {
    template.resourceCountIs("AWS::ElasticLoadBalancingV2::TargetGroup", 1);
    template.hasResourceProperties("AWS::ElasticLoadBalancingV2::TargetGroup", {
      HealthCheckPath: API_HEALTH_CHECK_PATH,
      Port: API_CONTAINER_PORT,
      Protocol: "HTTP",
    });
    template.hasResourceProperties("AWS::ElasticLoadBalancingV2::Listener", {
      DefaultActions: [{ TargetGroupArn: Match.anyValue(), Type: "forward" }],
      Protocol: "HTTPS",
    });
  });

  it("redirects the HTTP listener to HTTPS", () => {
    template.hasResourceProperties("AWS::ElasticLoadBalancingV2::Listener", {
      DefaultActions: [
        {
          RedirectConfig: {
            Port: "443",
            Protocol: "HTTPS",
            StatusCode: "HTTP_301",
          },
          Type: "redirect",
        },
      ],
      Port: 80,
      Protocol: "HTTP",
    });
  });

  it("allows 443 from the VPC CIDR only", () => {
    const rules = ingressRules(template);
    const httpsRules = rules.filter((rule) => rule.FromPort === 443);

    expect(httpsRules).toHaveLength(1);
    expect(httpsRules[0]).toMatchObject({
      CidrIp: "10.0.0.0/16",
      IpProtocol: "tcp",
      ToPort: 443,
    });
    for (const rule of rules) {
      expect(rule).not.toHaveProperty("CidrIp", "0.0.0.0/0");
      expect(rule).not.toHaveProperty("CidrIpv6", "::/0");
    }
  });
});
