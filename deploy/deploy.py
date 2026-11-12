"""Build and deploy the current git commit to AWS Lambda (repeatable, data-safe: data lives in RDS + S3).
Usage: python3 deploy/deploy.py [git-ref] [--bootstrap]
  git-ref      default HEAD
  --bootstrap  first deploy only: pass the shared MySQL server's admin login so the app can create its own
               `intranet` database + user, and import the old Lightsail data. Redeploy without it afterwards.
Run deploy/aws_setup.py once first. Steps: git archive -> S3 -> CodeBuild image -> Lambda + Function URL."""
import json, subprocess, sys, time, boto3
from botocore.exceptions import ClientError

R = "eu-west-2"; NAME = "demeisen-intranet"; DB_SERVER = "demeisen-crm-poc"
ACCT = boto3.client("sts").get_caller_identity()["Account"]
FILES = f"{NAME}-files-{ACCT}"; BUILD = f"{NAME}-build-{ACCT}"
cb = boto3.client("codebuild", region_name=R); lam = boto3.client("lambda", region_name=R)
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
    img = ecr.describe_images(repositoryName=NAME, imageIds=[{"imageTag": "latest"}])["imageDetails"][0]
    repo = ecr.describe_repositories(repositoryNames=[NAME])["repositories"][0]["repositoryUri"]
    return sha, f"{repo}@{img['imageDigest']}"


def secret(arn, key=None):
    v = sm.get_secret_value(SecretId=arn)["SecretString"]
    return json.loads(v)[key] if key else v


def exec_role():
    name = f"{NAME}-lambda"
    try: return iam.get_role(RoleName=name)["Role"]["Arn"]
    except ClientError: pass
    arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}))["Role"]["Arn"]
    iam.attach_role_policy(RoleName=name, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole")
    iam.put_role_policy(RoleName=name, PolicyName="files", PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": f"arn:aws:s3:::{FILES}/*"},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{FILES}"}]}))
    time.sleep(15)
    return arn


def env(bootstrap):
    d = rds.describe_db_instances(DBInstanceIdentifier=DB_SERVER)["DBInstances"][0]
    e = {"DB_HOST": d["Endpoint"]["Address"], "DB_USER": "intranet", "DB_NAME": "intranet",
         "DB_PASSWORD": secret(f"{NAME}/db-password"), "INTRANET_SECRET": secret(f"{NAME}/flask-secret"),
         "FILES_BUCKET": FILES}
    if bootstrap:
        e.update(DB_ADMIN_USER=d["MasterUsername"], DB_ADMIN_PASSWORD=secret(d["MasterUserSecret"]["SecretArn"], "password"),
                 IMPORT_SQLITE_S3_KEY="migration/intranet.db")
    return {"Variables": e}


def vpc_config():
    vpc = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
    subnets = [s["SubnetId"] for s in ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]]
    sg = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{NAME}-app"]},
                                               {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"][0]["GroupId"]
    return {"SubnetIds": subnets, "SecurityGroupIds": [sg]}


def settle():
    lam.get_waiter("function_updated_v2").wait(FunctionName=NAME)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    bootstrap = "--bootstrap" in sys.argv
    sha, image = build(args[0] if args else "HEAD")
    cfg = dict(Role=exec_role(), Timeout=120, MemorySize=2048, EphemeralStorage={"Size": 1024},
               Environment=env(bootstrap), VpcConfig=vpc_config())
    try:
        lam.get_function(FunctionName=NAME)
        lam.update_function_code(FunctionName=NAME, ImageUri=image); settle()
        lam.update_function_configuration(FunctionName=NAME, **cfg); settle()
    except lam.exceptions.ResourceNotFoundException:
        lam.create_function(FunctionName=NAME, PackageType="Image", Code={"ImageUri": image},
                            Architectures=["x86_64"], **cfg)
        lam.get_waiter("function_active_v2").wait(FunctionName=NAME)
    try: url = lam.get_function_url_config(FunctionName=NAME)["FunctionUrl"]
    except lam.exceptions.ResourceNotFoundException:
        url = lam.create_function_url_config(FunctionName=NAME, AuthType="NONE")["FunctionUrl"]
        lam.add_permission(FunctionName=NAME, StatementId="public-url", Action="lambda:InvokeFunctionUrl",
                           Principal="*", FunctionUrlAuthType="NONE")
        lam.add_permission(FunctionName=NAME, StatementId="public-url-invoke", Action="lambda:InvokeFunction",
                           Principal="*", InvokedViaFunctionUrl=True)
    # smoke test from inside AWS (this sandbox can't reach the URL)
    r = lam.invoke(FunctionName=NAME, Payload=json.dumps({"version": "2.0", "rawPath": "/healthz", "rawQueryString": "",
        "headers": {"host": url.split("/")[2], "x-forwarded-proto": "https", "x-forwarded-port": "443"},
        "requestContext": {"http": {"method": "GET", "path": "/healthz", "sourceIp": "127.0.0.1",
                                    "protocol": "HTTP/1.1"}}, "isBase64Encoded": False}), LogType="Tail")
    out = json.loads(r["Payload"].read())
    print(json.dumps({"deployed": sha, "url": url, "health": out.get("statusCode"), "error": r.get("FunctionError")}))
    if out.get("statusCode") != 200:
        import base64; print(base64.b64decode(r["LogResult"]).decode()[-3000:]); sys.exit(1)


if __name__ == "__main__":
    main()
