#!/usr/bin/env python3
"""Run master bootstrap SQL against private RDS via ECS Fargate (in-VPC)."""

from __future__ import annotations

import base64
import json
import os
import sys
import time

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
PROFILE = os.environ.get("AWS_PROFILE", "aisuite-dev")
STACK = os.environ.get("STACK", "aisuite-dev-infrastructure")
CLUSTER = os.environ.get("CLUSTER", "aisuite-dev-rag-api")
MASTER_SECRET_ID = "aisuite/dev/rag-db-credentials"
APP_SECRET_ID = "aisuite/dev/rag-app-db-credentials"

BOOTSTRAP_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS embeddings (
    id SERIAL PRIMARY KEY,
    text TEXT NOT NULL,
    metadata JSONB DEFAULT NULL,
    embedding VECTOR(1536) NOT NULL,
    search_tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(text, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(metadata::text, '')), 'B')
    ) STORED,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw
    ON embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m=16, ef_construction=128);
CREATE INDEX IF NOT EXISTS idx_embeddings_metadata ON embeddings USING GIN (metadata);
CREATE INDEX IF NOT EXISTS idx_search_text_trgm ON embeddings USING GIN (text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_search_tsv ON embeddings USING GIN (search_tsv);

SELECT set_config('app.password', :'pwd', false);
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'aisuite_app') THEN
    EXECUTE format('CREATE ROLE aisuite_app LOGIN PASSWORD %L', current_setting('app.password'));
  ELSE
    EXECUTE format('ALTER ROLE aisuite_app WITH LOGIN PASSWORD %L', current_setting('app.password'));
  END IF;
END
$$;

GRANT CONNECT ON DATABASE vectordb TO aisuite_app;
GRANT USAGE ON SCHEMA public TO aisuite_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aisuite_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aisuite_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO aisuite_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aisuite_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO aisuite_app;
"""


def session() -> boto3.Session:
    return boto3.Session(profile_name=PROFILE, region_name=REGION)


def secret_json(sm, secret_id: str) -> dict:
    return json.loads(sm.get_secret_value(SecretId=secret_id)["SecretString"])


def find_app_sg(ec2, cfn) -> str:
    resources = cfn.describe_stack_resources(StackName=STACK)["StackResources"]
    for resource in resources:
        if (
            resource["ResourceType"] == "AWS::EC2::SecurityGroup"
            and "AppSecurityGroup" in resource["LogicalResourceId"]
        ):
            return resource["PhysicalResourceId"]
    groups = ec2.describe_security_groups(
        Filters=[{"Name": "description", "Values": ["AISuite application tier."]}]
    )["SecurityGroups"]
    if not groups:
        raise RuntimeError("Could not find app security group")
    return groups[0]["GroupId"]


def find_private_subnets(ec2) -> list[str]:
    vpc_id = ec2.describe_vpcs(
        Filters=[{"Name": "tag:Name", "Values": ["aisuite-east-dev"]}]
    )["Vpcs"][0]["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])[
        "Subnets"
    ]
    private: list[str] = []
    for subnet in subnets:
        tags = {t["Key"]: t["Value"] for t in subnet.get("Tags", [])}
        name = tags.get("Name", "")
        if "Private" in name or tags.get("aws-cdk:subnet-type") == "Private":
            private.append(subnet["SubnetId"])
    if not private:
        raise RuntimeError("No private subnets found in aisuite-east-dev")
    return private[:3]


def find_execution_role(iam, ecs) -> str:
    defs = ecs.list_task_definitions(
        familyPrefix="aisuite-dev-rag-api", sort="DESC", maxResults=1
    ).get("taskDefinitionArns", [])
    if defs:
        td = ecs.describe_task_definition(taskDefinition=defs[0])["taskDefinition"]
        return td["executionRoleArn"]
    for role in iam.list_roles()["Roles"]:
        if "ComputeExecutionRole" in role["RoleName"]:
            return role["Arn"]
    raise RuntimeError("Could not find ECS execution role")


def main() -> int:
    sess = session()
    sm = sess.client("secretsmanager")
    ec2 = sess.client("ec2")
    cfn = sess.client("cloudformation")
    ecs = sess.client("ecs")
    iam = sess.client("iam")
    logs = sess.client("logs")

    master = secret_json(sm, MASTER_SECRET_ID)
    app = secret_json(sm, APP_SECRET_ID)

    sql_b64 = base64.b64encode(BOOTSTRAP_SQL.encode()).decode()
    command = [
        "bash",
        "-lc",
        (
            "set -euo pipefail; "
            f"echo {sql_b64} | base64 -d > /tmp/bootstrap.sql; "
            'psql -v ON_ERROR_STOP=1 -v pwd="$APP_PASS" -f /tmp/bootstrap.sql; '
            "echo BOOTSTRAP_OK"
        ),
    ]

    log_group = "/ecs/aisuite-dev-rag-db-bootstrap"
    try:
        logs.create_log_group(logGroupName=log_group)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass

    exec_role = find_execution_role(iam, ecs)
    app_sg = find_app_sg(ec2, cfn)
    subnets = find_private_subnets(ec2)

    td = ecs.register_task_definition(
        family="aisuite-dev-rag-db-bootstrap",
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu="256",
        memory="512",
        executionRoleArn=exec_role,
        containerDefinitions=[
            {
                "name": "bootstrap",
                "image": "public.ecr.aws/docker/library/postgres:16",
                "essential": True,
                "command": command,
                "environment": [
                    {"name": "PGHOST", "value": master["host"]},
                    {"name": "PGPORT", "value": str(master.get("port", 5432))},
                    {"name": "PGUSER", "value": master["username"]},
                    {
                        "name": "PGDATABASE",
                        "value": master.get("dbname")
                        or master.get("database")
                        or "vectordb",
                    },
                    {"name": "PGPASSWORD", "value": master["password"]},
                    {"name": "APP_PASS", "value": app["password"]},
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": log_group,
                        "awslogs-region": REGION,
                        "awslogs-stream-prefix": "bootstrap",
                    },
                },
            }
        ],
    )
    task_def_arn = td["taskDefinition"]["taskDefinitionArn"]
    print(f"Registered {task_def_arn}", flush=True)

    run = ecs.run_task(
        cluster=CLUSTER,
        launchType="FARGATE",
        taskDefinition=task_def_arn,
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnets,
                "securityGroups": [app_sg],
                "assignPublicIp": "DISABLED",
            }
        },
    )
    if run.get("failures"):
        print(json.dumps(run["failures"], indent=2), file=sys.stderr)
        return 1

    task_arn = run["tasks"][0]["taskArn"]
    print(f"Started {task_arn}", flush=True)

    desc = None
    while True:
        desc = ecs.describe_tasks(cluster=CLUSTER, tasks=[task_arn])["tasks"][0]
        last = desc["lastStatus"]
        print(f"status={last}", flush=True)
        if last == "STOPPED":
            break
        time.sleep(5)

    exit_code = desc["containers"][0].get("exitCode")
    print(f"exitCode={exit_code} stoppedReason={desc.get('stoppedReason')}", flush=True)

    streams = logs.describe_log_streams(
        logGroupName=log_group, orderBy="LastEventTime", descending=True, limit=1
    ).get("logStreams", [])
    if streams:
        events = logs.get_log_events(
            logGroupName=log_group,
            logStreamName=streams[0]["logStreamName"],
            startFromHead=False,
            limit=80,
        )["events"]
        for event in events:
            print(event["message"].rstrip(), flush=True)

    return 0 if exit_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
