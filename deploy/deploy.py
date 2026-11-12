"""Build and deploy the current git commit to App Runner (repeatable, data-safe: data lives in RDS + S3).
Usage: python3 deploy/deploy.py [git-ref]        (default HEAD; run deploy/aws_setup.py once first)
Steps: git archive -> S3 -> CodeBuild builds/pushes image -> App Runner created or redeployed."""
import io, json, subprocess, sys, time, boto3

R = "eu-west-2"; NAME = "demeisen-intranet"; DB_SERVER = "demeisen-crm-poc"
ACCT = boto3.client("sts").get_caller_identity()["Account"]
FILES = f"{NAME}-files-{ACCT}"; BUILD = f"{NAME}-build-{ACCT}"
cb = boto3.client("codebuild", region_name=R); ar = boto3.client("apprunner", region_name=R)
rds = boto3.client("rds", region_name=R); sm = boto3.client("secretsmanager", region_name=R)
iam = boto3.client("iam"); ec2 = boto3.client("ec2", region_name=R); ecr = boto3.client("ecr", region_name=R)


def build(ref):
    sha = subprocess.check_output(["git", "rev-parse", "--short", ref], text=True).strip()
    z = subprocess.check_output(["git", "archive", "--format=zip", ref])
    boto3.client("s3", region_name=R).put_object(Bucket=BUILD, Key="source.zip", Body=z)
    b = cb.start_build(projectName=NAME)["build"]["id"]
    print(f"building {sha} ({b})", flush=True)
    while True:
        st = cb.batch_get_builds(ids=[b])["builds"][0]
        if st["buildStatus"] != "IN_PROGRESS": break
        time.sleep(15)
    if st["buildStatus"] != "SUCCEEDED":
        sys.exit(f"build {st['buildStatus']}: see CodeBuild logs for {b}")
    return sha


def service_arn():
    for s in ar.list_services()["ServiceSummaryList"]:
        if s["ServiceName"] == NAME: return s["ServiceArn"]


def wait(arn):
    while True:
        s = ar.describe_service(ServiceArn=arn)["Service"]
        if s["Status"] != "OPERATION_IN_PROGRESS": return s
        time.sleep(20)


def create_service(env_extra):
    d = rds.describe_db_instances(DBInstanceIdentifier=DB_SERVER)["DBInstances"][0]
    db_secret = sm.describe_secret(SecretId=f"{NAME}/db-password")["ARN"]
    admin = d["MasterUserSecret"]["SecretArn"]
    flask_secret = sm.describe_secret(SecretId=f"{NAME}/flask-secret")["ARN"]
    vpc = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
    subnets = [s["SubnetId"] for s in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]]
    app_sg = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{NAME}-app"]}])["SecurityGroups"][0]["GroupId"]
    conns = [c for c in ar.list_vpc_connectors()["VpcConnectors"] if c["VpcConnectorName"] == NAME and c["Status"] == "ACTIVE"]
    conn = conns[0]["VpcConnectorArn"] if conns else ar.create_vpc_connector(
        VpcConnectorName=NAME, Subnets=subnets, SecurityGroups=[app_sg])["VpcConnector"]["VpcConnectorArn"]
    repo = ecr.describe_repositories(repositoryNames=[NAME])["repositories"][0]["repositoryUri"]
    env = {"DB_HOST": d["Endpoint"]["Address"], "DB_USER": "intranet", "DB_NAME": "intranet",
           "FILES_BUCKET": FILES, "AWS_REGION": R, "DB_ADMIN_USER": d["MasterUsername"], **env_extra}
    return ar.create_service(ServiceName=NAME,
        SourceConfiguration={"AutoDeploymentsEnabled": False,
            "AuthenticationConfiguration": {"AccessRoleArn": iam.get_role(RoleName=f"{NAME}-apprunner-ecr")["Role"]["Arn"]},
            "ImageRepository": {"ImageIdentifier": f"{repo}:latest", "ImageRepositoryType": "ECR",
                "ImageConfiguration": {"Port": "8080", "RuntimeEnvironmentVariables": env,
                    "RuntimeEnvironmentSecrets": {"DB_PASSWORD": db_secret, "DB_ADMIN_PASSWORD": f"{admin}:password::",
                                                  "INTRANET_SECRET": flask_secret}}}},
        InstanceConfiguration={"Cpu": "1 vCPU", "Memory": "2 GB",
                               "InstanceRoleArn": iam.get_role(RoleName=f"{NAME}-apprunner-instance")["Role"]["Arn"]},
        NetworkConfiguration={"EgressConfiguration": {"EgressType": "VPC", "VpcConnectorArn": conn}},
        HealthCheckConfiguration={"Protocol": "HTTP", "Path": "/healthz", "Interval": 10, "Timeout": 5,
                                  "HealthyThreshold": 1, "UnhealthyThreshold": 5},
        AutoScalingConfigurationArn=_autoscaling())["Service"]["ServiceArn"]


def _autoscaling():
    for c in ar.list_auto_scaling_configurations(AutoScalingConfigurationName=NAME)["AutoScalingConfigurationSummaryList"]:
        return c["AutoScalingConfigurationArn"]
    return ar.create_auto_scaling_configuration(AutoScalingConfigurationName=NAME, MaxConcurrency=50,
        MinSize=1, MaxSize=1)["AutoScalingConfiguration"]["AutoScalingConfigurationArn"]


def main():
    ref = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
    sha = build(ref)
    arn = service_arn()
    if arn:
        wait(arn); ar.start_deployment(ServiceArn=arn); time.sleep(5)
    else:
        arn = create_service({"IMPORT_SQLITE_S3_KEY": "migration/intranet.db"})
    s = wait(arn)
    print(json.dumps({"deployed": sha, "status": s["Status"], "url": "https://" + s["ServiceUrl"]}))
    if s["Status"] != "RUNNING": sys.exit(1)


if __name__ == "__main__":
    main()
