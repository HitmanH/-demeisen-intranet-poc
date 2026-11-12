"""Idempotent setup of the intranet's AWS stack (account 642552605309, eu-west-2):
private S3 bucket, ECR repo, CodeBuild image build, RDS MySQL, secrets, IAM role for CodeBuild. The Lambda function itself is created by deploy/deploy.py.
Safe to re-run: existing resources are left as they are. Never deletes anything.
Usage: python3 deploy/aws_setup.py    (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in env)"""
import json, secrets, time, boto3
from botocore.exceptions import ClientError

R = "eu-west-2"; NAME = "demeisen-intranet"; DB_SERVER = "demeisen-crm-poc"
ACCT = boto3.client("sts").get_caller_identity()["Account"]
FILES = f"{NAME}-files-{ACCT}"; BUILD = f"{NAME}-build-{ACCT}"
s3 = boto3.client("s3", region_name=R); iam = boto3.client("iam"); ec2 = boto3.client("ec2", region_name=R)
rds = boto3.client("rds", region_name=R); sm = boto3.client("secretsmanager", region_name=R)
ecr = boto3.client("ecr", region_name=R); cb = boto3.client("codebuild", region_name=R)


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
    app_sg = sg(f"{NAME}-app", "intranet Lambda")
    rts = [t["RouteTableId"] for t in ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["RouteTables"]]
    if not ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc]},
                                               {"Name": "service-name", "Values": [f"com.amazonaws.{R}.s3"]}])["VpcEndpoints"]:
        ec2.create_vpc_endpoint(VpcId=vpc, ServiceName=f"com.amazonaws.{R}.s3", RouteTableIds=rts)

    # --- shared RDS MySQL server (also hosts the CRM); intranet gets its own database + user
    d = rds.describe_db_instances(DBInstanceIdentifier=DB_SERVER)["DBInstances"][0]
    for g in d["VpcSecurityGroups"]:   # allow MySQL from the intranet app only (additive)
        exists(lambda: ec2.authorize_security_group_ingress(GroupId=g["VpcSecurityGroupId"], IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 3306, "ToPort": 3306,
            "UserIdGroupPairs": [{"GroupId": app_sg, "Description": "demeisen-intranet Lambda"}]}]),
            "InvalidPermission.Duplicate")
    db_admin_secret = d["MasterUserSecret"]["SecretArn"]
    if not exists(lambda: sm.describe_secret(SecretId=f"{NAME}/db-password"), "ResourceNotFoundException"):
        sm.create_secret(Name=f"{NAME}/db-password", SecretString=secrets.token_urlsafe(24).replace("-", "x"))
    db_secret = sm.describe_secret(SecretId=f"{NAME}/db-password")["ARN"]

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

    print(json.dumps(dict(repo=repo, db_host=d["Endpoint"]["Address"], app_sg=app_sg, files=FILES, build=BUILD)))

if __name__ == "__main__":
    main()
