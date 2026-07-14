import * as cdk from "aws-cdk-lib";

import { AisuiteInfrastructureStack } from "../src/aisuite-infrastructure-stack";
import { getDeploymentConfig } from "../src/deployment-config";

const app = new cdk.App();
const requestedEnvironmentContext = app.node.tryGetContext("environment");
const requestedEnvironment =
  typeof requestedEnvironmentContext === "string"
    ? requestedEnvironmentContext
    : process.env.DEPLOYMENT_ENV ?? "dev";
const deploymentConfig = getDeploymentConfig(requestedEnvironment);

// Pin DEPLOYMENT_TIMESTAMP in CI (e.g. the release/commit time) so the tag is
// stable within a release. Without it every synth produces a new value, which
// makes CloudFormation report a tag change on every resource on each deploy.
const deployedAt = process.env.DEPLOYMENT_TIMESTAMP ?? new Date().toISOString();

new AisuiteInfrastructureStack(app, deploymentConfig.stackName, {
  deploymentConfig,
  env: deploymentConfig.awsEnvironment,
  stackName: deploymentConfig.stackName,
  tags: {
    ...deploymentConfig.tags,
    DeployedAt: deployedAt,
  },
});

app.synth();
