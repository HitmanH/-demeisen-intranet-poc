# Demeisen intranet – notes for Claude sessions

Flask + SQLite app. See README.md for features.

## Live deployment (AWS, account of russell.b.hart)
- Lightsail instance `demeisen-intranet`, region eu-west-2 (London), Ubuntu 24.04, bundle small_3_0,
  static IP `demeisen-intranet-ip`. Only port 80 open (SSH deliberately closed).
- Lightsail distribution `demeisen-intranet-cdn` (API region us-east-1) → https://d82v7tclsph37.cloudfront.net
- App lives in `/opt/intranet` on the server, runs as systemd service `intranet` (gunicorn on :80,
  user `intranet`, env INTRANET_PROD=1 and a random INTRANET_SECRET in the unit file).
- **Live data is ONLY on the server**: `/opt/intranet/data/intranet.db` (+ `data/generated`, `data/uploads`).
  It is not in git.

## Rules
- NEVER delete/recreate the instance or overwrite `/opt/intranet/data` – that destroys all records.
- Don't reopen SSH to 0.0.0.0/0. Take a Lightsail snapshot before any server change.
- AWS creds come from env vars AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (boto3; aws CLI not installed).
- S3 presigned URLs must use the regional endpoint + SigV4
  (`endpoint_url="https://s3.eu-west-2.amazonaws.com"`, `Config(signature_version="s3v4")`), otherwise
  the server's download fails (307 redirect) – this caused the first 504.
- This sandbox cannot reach cloudfront.net or the server IP directly; verify via the server uploading a
  status file to a presigned S3 PUT URL, or ask the user to check.

## Pending task
The live server runs the first commit. Deploy the latest `main` (NDA layout fixes) **in place**, and set up a
repeatable, data-preserving update method (e.g. Lightsail snapshot, then a temporary SSH rule limited to the
session's egress IP or a pull-based updater) – code files only, keep `data/`.
