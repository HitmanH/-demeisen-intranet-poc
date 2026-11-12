# Demeisen intranet – notes for Claude sessions

Flask app. See README.md for features. Locally it uses SQLite (`data/intranet.db`); in AWS it uses MySQL + S3.

## Live deployment (AWS account 642552605309, eu-west-2)
- **AWS Lambda** function `demeisen-intranet` (container image built by CodeBuild project `demeisen-intranet`,
  stored in ECR repo `demeisen-intranet`), public Function URL:
  https://horhqpmqdobjadyvd3sk2uz4qi0dthvi.lambda-url.eu-west-2.on.aws/
  (App Runner was the plan, but AWS no longer lets this account create App Runner services.)
- **Database**: MySQL database `intranet` on the shared RDS server `demeisen-crm-poc` (the CRM's server;
  CRM has its own database there). App login `intranet` has rights on `intranet.*` only; its password is
  secret `demeisen-intranet/db-password`. Flask secret: `demeisen-intranet/flask-secret`. RDS keeps 7-day backups.
- **Files** (generated NDAs/PDFs, uploads): private S3 bucket `demeisen-intranet-files-642552605309`
  (`generated/`, `uploads/`, versioned). `migration/` holds the one-off export from Lightsail.
- Lambda runs in the default VPC (SG `demeisen-intranet-app`, allowed into the RDS SG on 3306), reaches S3 via
  the VPC gateway endpoint. Limits: ~6 MB per upload (Function URL payload), cold start of a few seconds.
- CRM link on the home page: `config.LINKS`, override with env `CRM_URL` (target later: crm.demeisen.co.uk;
  intranet later: intranet.demeisen.co.uk).

## Deploying
`python3 deploy/deploy.py` (builds the current commit in CodeBuild, updates Lambda, smoke-tests /healthz).
`deploy/aws_setup.py` is the idempotent one-time infrastructure setup. Data is never touched by a deploy.
Never pass `--bootstrap` again: it gives the app the RDS admin login and was only for the first deploy.
This sandbox can't open the Function URL; test via `lambda.invoke` (see deploy.py) or ask the user.

## Rules
- Never drop/overwrite the `intranet` database or the files bucket. Never touch the CRM's database on the shared server.
- AWS creds come from env vars AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (boto3; aws CLI not installed).
- S3 presigned URLs must use the regional endpoint + SigV4 (`storage.py` does this).

## Old Lightsail setup (being retired)
Instance `demeisen-intranet` + distribution `demeisen-intranet-cdn` (https://d82v7tclsph37.cloudfront.net) still exist,
with auto-snapshots and snapshots `demeisen-intranet-pre-deploy-20261111`, `-pre-migration-20261111`,
`-export-20261112` (data exported from the last one on 2026-11-12). Keep them until the user confirms the migrated
data is complete; then stop (not delete) first. `deploy/update.sh` / `console_ssh.py` relate only to Lightsail.
