import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";

import type { DeploymentConfig } from "./deployment-config";

export interface AisuiteInfrastructureStackProps extends cdk.StackProps {
  deploymentConfig: DeploymentConfig;
}

export class AisuiteInfrastructureStack extends cdk.Stack {
  public constructor(
    scope: Construct,
    id: string,
    props: AisuiteInfrastructureStackProps,
  ) {
    super(scope, id, props);

    new cdk.CfnOutput(this, "DeploymentEnvironment", {
      description: "AISuite deployment environment represented by this stack.",
      value: props.deploymentConfig.name,
    });

    new cdk.CfnOutput(this, "ProtectedEnvironment", {
      description:
        "Whether deployment should require GitHub environment protection.",
      value: String(props.deploymentConfig.protectedEnvironment),
    });
  }
}
