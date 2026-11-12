# Demeisen intranet – notes for Claude sessions

Flask app. See README.md for features. Locally it uses SQLite (`data/intranet.db`); in AWS it uses MySQL + S3.

## Live deployment (AWS account 642552605309, eu-west-2)
- **Address**: https://d1498m8yxqclgy.cloudfront.net (CloudFront, comment `demeisen-intranet`, no caching).
  `/auth/*` goes to Lambda `demeisen-intranet-auth` (sign-in only, outside the VPC so it can reach Cognito, no DB);
  everything else goes to Lambda `demeisen-intranet` (in the VPC). Both run the same image (ECR `demeisen-intranet`,
  built by CodeBuild project `demeisen-intranet`). The raw Function URLs work but sign-in only works via CloudFront.
- **Sign-in**: shared Cognito user pool `eu-west-2_o84PDILyl` (created by the CRM's stack `demeisen-crm-poc`; hosted
  page `demeisen-crm-poc.auth.eu-west-2.amazoncognito.com`). The intranet's app client comes from stack
  `demeisen-intranet-signin` (template `cognito/app-client.yaml` in repo HitmanH/demeisen-auth). Code:
  `demeisen_auth.py` (copied from that repo). Signed-in emails are admitted only if active in the intranet's `users`
  table. Without OIDC_* env vars the app falls back to its built-in passwords (local dev / standalone use).
- **Look**: shared UI copied into `static/ui/` from repo HitmanH/demeisen-ui (`vendor.sh <ref> static/ui`);
  intranet-only styles in `static/style.css`. Header app switcher links: `config.APPS` (CRM_URL env overrides).
- **Database**: MySQL database `intranet` on the shared RDS server `demeisen-crm-poc` (the CRM's server;
  CRM has its own database there). App login `intranet` has rights on `intranet.*` only; its password is
  secret `demeisen-intranet/db-password`. Flask secret: `demeisen-intranet/flask-secret`. RDS keeps 7-day backups.
- **Files** (generated NDAs/PDFs, uploads): private S3 bucket `demeisen-intranet-files-642552605309`
  (`generated/`, `uploads/`, versioned). `migration/` holds the one-off export from Lightsail (full backup of it).
- Limits: ~6 MB per upload (Lambda payload), cold start of a few seconds.
- Admin tasks (list/delete NDA) via direct `lambda.invoke` with `{"admin_task": ...}`: see `lambda_handler.py`.

## Deploying
`python3 deploy/deploy.py` (builds the current commit in CodeBuild, updates Lambda, smoke-tests /healthz).
`deploy/aws_setup.py` is the idempotent one-time infrastructure setup. Data is never touched by a deploy.
Never pass `--bootstrap` again: it gives the app the RDS admin login and was only for the first deploy.
This sandbox can't open the Function URL; test via `lambda.invoke` (see deploy.py) or ask the user.

## Rules
- Never drop/overwrite the `intranet` database or the files bucket. Never touch the CRM's database on the shared server.
- AWS creds come from env vars AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (boto3; aws CLI not installed).
- S3 presigned URLs must use the regional endpoint + SigV4 (`storage.py` does this).

## History
Lightsail (first host) was deleted on 2026-11-12 after migration, with its snapshots; its data is in S3 `migration/`.
