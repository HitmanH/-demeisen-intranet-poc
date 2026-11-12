# Deploying updates

The intranet runs on AWS Lambda with data in RDS MySQL and S3, so a deploy only replaces code. Data is never touched.

```
python3 deploy/deploy.py            # deploy the current commit (needs AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
python3 deploy/deploy.py <git-ref>  # or a specific branch/commit
```
It builds the image in CodeBuild, points Lambda at it, and checks `/healthz`. To roll back, deploy the previous commit.

`aws_setup.py` creates the infrastructure (idempotent; only needed once). The deploy also updates the sign-in
function and CloudFront routing. To update the shared look: `../demeisen-ui/vendor.sh <ref> static/ui`, commit, deploy.
