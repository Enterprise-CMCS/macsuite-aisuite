import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";

import { DEFAULT_REGION } from "../deployment-config";

export const STUB_VPC_CONTEXT_KEY = "aisuite:stubVpc";

export const POSTGRES_PORT = 5432;

export interface NetworkingConstructProps {
  vpcName: string;
  vpc?: ec2.IVpc;
}

export class NetworkingConstruct extends Construct {
  public readonly appSecurityGroup: ec2.SecurityGroup;
  public readonly dbSecurityGroup: ec2.SecurityGroup;
  public readonly vpc: ec2.IVpc;

  public constructor(
    scope: Construct,
    id: string,
    props: NetworkingConstructProps,
  ) {
    super(scope, id);

    this.vpc = props.vpc ?? resolveVpc(this, props.vpcName);

    this.appSecurityGroup = new ec2.SecurityGroup(this, "AppSecurityGroup", {
      allowAllOutbound: true,
      description: "AISuite application tier.",
      vpc: this.vpc,
    });

    this.dbSecurityGroup = new ec2.SecurityGroup(this, "DbSecurityGroup", {
      allowAllOutbound: false,
      description: "AISuite RAG PostgreSQL; ingress from application tier only.",
      vpc: this.vpc,
    });
  }
}

function resolveVpc(scope: Construct, vpcName: string): ec2.IVpc {
  if (isStubVpcRequested(scope)) {
    return stubVpc(scope);
  }

  return ec2.Vpc.fromLookup(scope, "Vpc", { vpcName });
}

/**
 * `cdk synth --context aisuite:stubVpc=true` yields the string `"true"`, while
 * programmatic `new App({ context })` yields a boolean. Accept both.
 */
function isStubVpcRequested(scope: Construct): boolean {
  const value: unknown = scope.node.tryGetContext(STUB_VPC_CONTEXT_KEY);

  return value === true || value === "true";
}

function stubVpc(scope: Construct): ec2.IVpc {
  return ec2.Vpc.fromVpcAttributes(scope, "StubVpc", {
    availabilityZones: [`${DEFAULT_REGION}a`, `${DEFAULT_REGION}b`],
    isolatedSubnetIds: ["subnet-0stubisolated1", "subnet-0stubisolated2"],
    isolatedSubnetRouteTableIds: ["rtb-0stubisolated1", "rtb-0stubisolated2"],
    privateSubnetIds: ["subnet-0stubprivate1", "subnet-0stubprivate2"],
    privateSubnetRouteTableIds: ["rtb-0stubprivate1", "rtb-0stubprivate2"],
    vpcCidrBlock: "10.0.0.0/16",
    vpcId: "vpc-0stubaisuitevpc",
  });
}
