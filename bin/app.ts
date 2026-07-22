import * as cdk from "aws-cdk-lib";

import { AisuiteInfrastructureStack } from "../src/aisuite-infrastructure-stack";
import { getDeploymentConfig } from "../src/deployment-config";

const app = new cdk.App();
const requestedEnvironmentContext = app.node.tryGetContext("environment");
const requestedEnvironment = requestedEnvironmentContext ?? "dev";
const deploymentConfig = getDeploymentConfig(requestedEnvironment);

new AisuiteInfrastructureStack(app, deploymentConfig.stackName, {
  deploymentConfig,
  env: deploymentConfig.awsEnvironment,
  stackName: deploymentConfig.stackName,
  tags: {
    ...deploymentConfig.tags,
  },
});

app.synth();
