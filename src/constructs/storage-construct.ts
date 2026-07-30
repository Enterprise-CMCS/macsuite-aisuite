import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";

import { APPLICATION_NAME, type DeploymentConfig } from "../deployment-config";

export interface StorageConstructProps {
  deploymentConfig: DeploymentConfig;
}

export class StorageConstruct extends Construct {
  public readonly buckets: s3.Bucket[];
  public readonly documentsBucket: s3.Bucket;
  public readonly postProcessingBucket: s3.Bucket;
  public readonly pipelineCodeBucket: s3.Bucket;
  public readonly pipelineTempBucket: s3.Bucket;

  public constructor(
    scope: Construct,
    id: string,
    props: StorageConstructProps,
  ) {
    super(scope, id);

    const { name, protectedEnvironment } = props.deploymentConfig;

    this.documentsBucket = this.createBucket(
      "DocumentsBucket",
      `${APPLICATION_NAME}-${name}-contract-rag`,
      protectedEnvironment,
    );
    this.postProcessingBucket = this.createBucket(
      "PostProcessingBucket",
      `${APPLICATION_NAME}-${name}-contract-rag-post-processing`,
      protectedEnvironment,
    );
    this.pipelineCodeBucket = this.createBucket(
      "PipelineCodeBucket",
      `${APPLICATION_NAME}-${name}-llm-pipeline-code`,
      protectedEnvironment,
    );
    this.pipelineTempBucket = this.createBucket(
      "PipelineTempBucket",
      `${APPLICATION_NAME}-${name}-llm-pipeline-temp`,
      protectedEnvironment,
    );

    this.buckets = [
      this.documentsBucket,
      this.postProcessingBucket,
      this.pipelineCodeBucket,
      this.pipelineTempBucket,
    ];
  }

  private createBucket(
    id: string,
    bucketName: string,
    protectedEnvironment: boolean,
  ): s3.Bucket {
    return new s3.Bucket(this, id, {
      autoDeleteObjects: !protectedEnvironment,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      bucketName,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: protectedEnvironment
        ? cdk.RemovalPolicy.RETAIN
        : cdk.RemovalPolicy.DESTROY,
      versioned: true,
    });
  }
}
