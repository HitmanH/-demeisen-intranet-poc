"""Idempotent setup of the intranet's AWS stack (account 642552605309, eu-west-2):
private S3 bucket, ECR repo, CodeBuild image build, RDS MySQL, secrets, IAM roles, App Runner.
Safe to re-run: existing resources are left as they are. Never deletes anything.
Usage: python3 deploy/aws_setup.py    (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in env)"""
import json, secrets, time, boto3
from botocore.exceptions import ClientError

R = "eu-west-2"; NAME = "demeisen-intranet"
ACCT = boto3.client("sts").get_caller_identity()["Account"]
FILES = f"{NAME}-files-{ACCT}"; BUILD = f"{NAME}-build-{ACCT}"
s3 = boto3.client("s3", region_name=R); iam = boto3.client("iam"); ec2 = boto3.client("ec2", region_name=R)
rds = boto3.client("rds", region_name=R); sm = boto3.client("secretsmanager", region_name=R)
ecr = boto3.client("ecr", region_name=R); cb = boto3.client("codebuild", region_name=R)
ar = boto3.client("apprunner", region_name=R)


def exists(fn, *codes):
    try: return fn()
    except ClientError as e:
        if e.response["Error"]["Code"] in codes: return None
        raise


def bucket(name, versioned):
    if not exists(lambda: s3.head_bucket(Bucket=name), "404", "NoSuchBucket"):
        s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": R})
    s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={k: True for k in (
        "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")})
    s3.put_bucket_encryption(Bucket=name, ServerSideEncryptionConfiguration={"Rules": [
        {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    if versioned: s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})


def role(name, service, policies=(), inline=None):
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service},
                                                     "Action": "sts:AssumeRole"}]}
    r = exists(lambda: iam.get_role(RoleName=name), "NoSuchEntity")
    if not r:
        r = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust)); time.sleep(10)
    for p in policies: iam.attach_role_policy(RoleName=name, PolicyArn=p)
    if inline: iam.put_role_policy(RoleName=name, PolicyName="inline", PolicyDocument=json.dumps(inline))
    return r["Role"]["Arn"]


def main():
    bucket(FILES, True); bucket(BUILD, False)
    if not exists(lambda: ecr.describe_repositories(repositoryNames=[NAME]), "RepositoryNotFoundException"):
        ecr.create_repository(repositoryName=NAME, imageScanningConfiguration={"scanOnPush": True})
    repo = ecr.describe_repositories(repositoryNames=[NAME])["repositories"][0]["repositoryUri"]

    # --- network: default VPC, SGs, S3 gateway endpoint (App Runner VPC egress has no internet)
    vpc = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
    subnets = [s["SubnetId"] for s in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]]

    def sg(n, desc):
        f = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [n]}, {"Name": "vpc-id", "Values": [vpc]}])
        return f["SecurityGroups"][0]["GroupId"] if f["SecurityGroups"] else \
            ec2.create_security_group(GroupName=n, Description=desc, VpcId=vpc)["GroupId"]
    app_sg = sg(f"{NAME}-app", "App Runner VPC connector"); db_sg = sg(f"{NAME}-db", "RDS, MySQL from app only")
    exists(lambda: ec2.authorize_security_group_ingress(GroupId=db_sg, IpPermissions=[{
        "IpProtocol": "tcp", "FromPort": 3306, "ToPort": 3306, "UserIdGroupPairs": [{"GroupId": app_sg}]}]),
        "InvalidPermission.Duplicate")
    rts = [t["RouteTableId"] for t in ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["RouteTables"]]
    if not ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc]},
                                               {"Name": "service-name", "Values": [f"com.amazonaws.{R}.s3"]}])["VpcEndpoints"]:
        ec2.create_vpc_endpoint(VpcId=vpc, ServiceName=f"com.amazonaws.{R}.s3", RouteTableIds=rts)

    # --- RDS MySQL (password managed by RDS in Secrets Manager)
    if not exists(lambda: rds.describe_db_subnet_groups(DBSubnetGroupName=NAME), "DBSubnetGroupNotFoundFault"):
        rds.create_db_subnet_group(DBSubnetGroupName=NAME, DBSubnetGroupDescription=NAME, SubnetIds=subnets)
    if not exists(lambda: rds.describe_db_instances(DBInstanceIdentifier=NAME), "DBInstanceNotFound"):
        rds.create_db_instance(DBInstanceIdentifier=NAME, Engine="mysql", EngineVersion="8.4",
            DBInstanceClass="db.t4g.micro", AllocatedStorage=20, StorageType="gp3", DBName="intranet",
            MasterUsername="intranet", ManageMasterUserPassword=True, DBSubnetGroupName=NAME,
            VpcSecurityGroupIds=[db_sg], PubliclyAccessible=False, BackupRetentionPeriod=7,
            PreferredBackupWindow="02:00-02:30", DeletionProtection=True, StorageEncrypted=True,
            CopyTagsToSnapshot=True, AutoMinorVersionUpgrade=True)
    print("waiting for RDS (can take ~10 min)..."); rds.get_waiter("db_instance_available").wait(
        DBInstanceIdentifier=NAME, WaiterConfig={"Delay": 30, "MaxAttempts": 60})
    d = rds.describe_db_instances(DBInstanceIdentifier=NAME)["DBInstances"][0]
    db_host, db_secret = d["Endpoint"]["Address"], d["MasterUserSecret"]["SecretArn"]

    if not exists(lambda: sm.describe_secret(SecretId=f"{NAME}/flask-secret"), "ResourceNotFoundException"):
        sm.create_secret(Name=f"{NAME}/flask-secret", SecretString=secrets.token_hex(32))
    flask_secret = sm.describe_secret(SecretId=f"{NAME}/flask-secret")["ARN"]

    # --- IAM
    build_role = role(f"{NAME}-codebuild", "codebuild.amazonaws.com", inline={"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"], "Resource": f"arn:aws:s3:::{BUILD}/*"},
        {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
        {"Effect": "Allow", "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
                                       "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage",
                                       "ecr:GetDownloadUrlForLayer"],
         "Resource": f"arn:aws:ecr:{R}:{ACCT}:repository/{NAME}"}]})
    access_role = role(f"{NAME}-apprunner-ecr", "build.apprunner.amazonaws.com",
                       ["arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"])
    inst_role = role(f"{NAME}-apprunner-instance", "tasks.apprunner.amazonaws.com", inline={"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": f"arn:aws:s3:::{FILES}/*"},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{FILES}"},
        {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": [db_secret, flask_secret]}]})

    # --- CodeBuild: builds the image from a source zip in the build bucket
    spec = """version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $REPO
  build:
    commands:
      - docker build -t $REPO:latest .
      - docker push $REPO:latest
"""
    proj = dict(name=NAME, source={"type": "S3", "location": f"{BUILD}/source.zip", "buildspec": spec},
                artifacts={"type": "NO_ARTIFACTS"}, serviceRole=build_role,
                environment={"type": "LINUX_CONTAINER", "image": "aws/codebuild/amazonlinux2-x86_64-standard:5.0",
                             "computeType": "BUILD_GENERAL1_SMALL", "privilegedMode": True,
                             "environmentVariables": [{"name": "REPO", "value": repo}]})
    (cb.update_project if cb.batch_get_projects(names=[NAME])["projects"] else cb.create_project)(**proj)

    # --- App Runner (created once the first image exists)
    print(json.dumps(dict(repo=repo, db_host=db_host, db_secret=db_secret, flask_secret=flask_secret,
                          access_role=access_role, inst_role=inst_role, app_sg=app_sg, subnets=subnets,
                          files=FILES, build=BUILD)))


if __name__ == "__main__":
    main()
