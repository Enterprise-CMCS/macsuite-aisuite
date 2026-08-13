import * as cdk from "aws-cdk-lib";
import * as rds from "aws-cdk-lib/aws-rds";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";

import { APPLICATION_NAME, type DeploymentConfig } from "../deployment-config";
import { DATABASE_NAME } from "./database-construct";

export const DB_ADMIN_USERNAME = "aisuite_admin";
export const DB_APP_USERNAME = "aisuite_app";

export interface SecretsConstructProps {
  deploymentConfig: DeploymentConfig;
}

export class SecretsConstruct extends Construct {
  public readonly dbSecret: secretsmanager.ISecret;
  public readonly appDbSecret: rds.DatabaseSecret;
  public readonly apiKeySecret: secretsmanager.ISecret;

  public constructor(
    scope: Construct,
    id: string,
    props: SecretsConstructProps,
  ) {
    super(scope, id);

    const { name, protectedEnvironment } = props.deploymentConfig;
    const removalPolicy = protectedEnvironment
      ? cdk.RemovalPolicy.RETAIN
      : cdk.RemovalPolicy.DESTROY;

    this.dbSecret = new secretsmanager.Secret(this, "RagDbCredentials", {
      description: `AISuite ${name} RAG PostgreSQL master credentials.`,
      generateSecretString: {
        excludePunctuation: true,
        generateStringKey: "password",
        passwordLength: 32,
        secretStringTemplate: JSON.stringify({ username: DB_ADMIN_USERNAME }),
      },
      removalPolicy,
      secretName: `${APPLICATION_NAME}/${name}/rag-db-credentials`,
    });

    this.appDbSecret = new rds.DatabaseSecret(this, "RagAppDbCredentials", {
      dbname: DATABASE_NAME,
      masterSecret: this.dbSecret,
      secretName: `${APPLICATION_NAME}/${name}/rag-app-db-credentials`,
      username: DB_APP_USERNAME,
    });
    this.appDbSecret.applyRemovalPolicy(removalPolicy);

    this.apiKeySecret = new secretsmanager.Secret(this, "RagApiKey", {
      description: `AISuite ${name} RAG API key.`,
      generateSecretString: {
        excludePunctuation: true,
        generateStringKey: "apiKey",
        passwordLength: 32,
        secretStringTemplate: JSON.stringify({}),
      },
      removalPolicy,
      secretName: `${APPLICATION_NAME}/${name}/rag-api-key`,
    });
  }
}
