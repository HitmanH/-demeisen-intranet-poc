"""Build and deploy the current git commit to AWS Lambda (repeatable, data-safe: data lives in RDS + S3).
Usage: python3 deploy/deploy.py [git-ref] [--bootstrap]
  git-ref      default HEAD
  --bootstrap  first deploy only: pass the shared MySQL server's admin login so the app can create its own
               `intranet` database + user, and import the old Lightsail data. Redeploy without it afterwards.
Run deploy/aws_setup.py once first. Steps: git archive -> S3 -> CodeBuild image -> Lambda + Function URL."""
import json, subprocess, sys, time, boto3
from botocore.exceptions import ClientError

R = "eu-west-2"; NAME = "demeisen-intranet"; DB_SERVER = "demeisen-crm-poc"
# Shared sign-in (see the demeisen-auth repo). Leave SIGNIN_STACK empty to use built-in passwords instead.
USER_POOL = "eu-west-2_o84PDILyl"; SIGNIN_DOMAIN = "demeisen-crm-poc"; SIGNIN_STACK = "demeisen-intranet-signin"
AUTH_FN = f"{NAME}-auth"
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


def public_url():
    for d in boto3.client("cloudfront").list_distributions().get("DistributionList", {}).get("Items", []):
        if d.get("Comment") == NAME: return "https://" + d["DomainName"]


def signin_env(with_secret):
    if not SIGNIN_STACK: return {}
    cid = {o["OutputKey"]: o["OutputValue"] for o in boto3.client("cloudformation", region_name=R)
           .describe_stacks(StackName=SIGNIN_STACK)["Stacks"][0]["Outputs"]}["ClientId"]
    e = {"OIDC_ISSUER": f"https://cognito-idp.{R}.amazonaws.com/{USER_POOL}", "OIDC_CLIENT_ID": cid,
         "OIDC_LOGOUT_URL": f"https://{SIGNIN_DOMAIN}.auth.{R}.amazoncognito.com/logout", "PUBLIC_URL": public_url()}
    if with_secret:
        e["OIDC_CLIENT_SECRET"] = boto3.client("cognito-idp", region_name=R).describe_user_pool_client(
            UserPoolId=USER_POOL, ClientId=cid)["UserPoolClient"]["ClientSecret"]
    return e


def env(bootstrap):
    d = rds.describe_db_instances(DBInstanceIdentifier=DB_SERVER)["DBInstances"][0]
    e = {"DB_HOST": d["Endpoint"]["Address"], "DB_USER": "intranet", "DB_NAME": "intranet",
         "DB_PASSWORD": secret(f"{NAME}/db-password"), "INTRANET_SECRET": secret(f"{NAME}/flask-secret"),
         "FILES_BUCKET": FILES, **signin_env(False)}
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


def settle(fn=NAME):
    lam.get_waiter("function_updated_v2").wait(FunctionName=fn)


def upsert(fn, image, cfg):
    try:
        lam.get_function(FunctionName=fn)
        lam.update_function_code(FunctionName=fn, ImageUri=image); settle(fn)
        lam.update_function_configuration(FunctionName=fn, **cfg); settle(fn)
    except lam.exceptions.ResourceNotFoundException:
        lam.create_function(FunctionName=fn, PackageType="Image", Code={"ImageUri": image}, Architectures=["x86_64"], **cfg)
        lam.get_waiter("function_active_v2").wait(FunctionName=fn)
    try: return lam.get_function_url_config(FunctionName=fn)["FunctionUrl"]
    except lam.exceptions.ResourceNotFoundException:
        url = lam.create_function_url_config(FunctionName=fn, AuthType="NONE")["FunctionUrl"]
        lam.add_permission(FunctionName=fn, StatementId="public-url", Action="lambda:InvokeFunctionUrl",
                           Principal="*", FunctionUrlAuthType="NONE")
        lam.add_permission(FunctionName=fn, StatementId="public-url-invoke", Action="lambda:InvokeFunction",
                           Principal="*", InvokedViaFunctionUrl=True)
        return url


def auth_role():
    name = f"{AUTH_FN}-lambda"
    try: return iam.get_role(RoleName=name)["Role"]["Arn"]
    except ClientError: pass
    arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}))["Role"]["Arn"]
    iam.attach_role_policy(RoleName=name, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    time.sleep(15)
    return arn


def route_auth(auth_url):
    """CloudFront: send /auth/* to the sign-in function (it can reach the provider; the main one is in the VPC)."""
    cf = boto3.client("cloudfront")
    d = next(x for x in cf.list_distributions()["DistributionList"]["Items"] if x.get("Comment") == NAME)
    res = cf.get_distribution_config(Id=d["Id"]); cfg, etag = res["DistributionConfig"], res["ETag"]
    host = auth_url.split("/")[2]
    if any(o["Id"] == "auth" for o in cfg["Origins"]["Items"]): return
    o = json.loads(json.dumps(cfg["Origins"]["Items"][0])); o.update(Id="auth", DomainName=host)
    cfg["Origins"]["Items"].append(o); cfg["Origins"]["Quantity"] += 1
    b = json.loads(json.dumps(cfg["DefaultCacheBehavior"])); b.update(PathPattern="/auth/*", TargetOriginId="auth")
    cb_ = cfg.get("CacheBehaviors") or {"Quantity": 0}
    cb_["Items"] = cb_.get("Items", []) + [b]; cb_["Quantity"] = len(cb_["Items"]); cfg["CacheBehaviors"] = cb_
    cf.update_distribution(Id=d["Id"], IfMatch=etag, DistributionConfig=cfg)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    bootstrap = "--bootstrap" in sys.argv
    sha, image = build(args[0] if args else "HEAD")
    cfg = dict(Role=exec_role(), Timeout=120, MemorySize=2048, EphemeralStorage={"Size": 1024},
               Environment=env(bootstrap), VpcConfig=vpc_config())
    url = upsert(NAME, image, cfg)
    if SIGNIN_STACK:   # sign-in-only copy of the app, outside the VPC, no database access
        auth_url = upsert(AUTH_FN, image, dict(Role=auth_role(), Timeout=30, MemorySize=512, Environment={"Variables": {
            "AUTH_ONLY": "1", "INTRANET_SECRET": secret(f"{NAME}/flask-secret"), **signin_env(True)}}))
        route_auth(auth_url)
    # smoke test from inside AWS (this sandbox can't reach the URL)
    r = lam.invoke(FunctionName=NAME, Payload=json.dumps({"version": "2.0", "rawPath": "/healthz", "rawQueryString": "",
        "headers": {"host": url.split("/")[2], "x-forwarded-proto": "https", "x-forwarded-port": "443"},
        "requestContext": {"http": {"method": "GET", "path": "/healthz", "sourceIp": "127.0.0.1",
                                    "protocol": "HTTP/1.1"}}, "isBase64Encoded": False}), LogType="Tail")
    out = json.loads(r["Payload"].read())
    print(json.dumps({"deployed": sha, "url": public_url() or url, "health": out.get("statusCode"), "error": r.get("FunctionError")}))
    if out.get("statusCode") != 200:
        import base64; print(base64.b64decode(r["LogResult"]).decode()[-3000:]); sys.exit(1)


if __name__ == "__main__":
    main()
