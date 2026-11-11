"""File storage: local data/ dirs, or a private S3 bucket when FILES_BUCKET is set.
Files are always produced in the local working dirs first, then pushed to S3."""
import os
BUCKET = os.environ.get("FILES_BUCKET")
REGION = os.environ.get("AWS_REGION", "eu-west-2")
_s3 = None


def s3():
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config
        _s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com",
                           config=Config(signature_version="s3v4"))
    return _s3


def put(kind, path):
    """Persist a file that was just written to the local <kind> dir."""
    if BUCKET:
        s3().upload_file(path, BUCKET, f"{kind}/{os.path.basename(path)}")


def download_url(kind, name):
    return s3().generate_presigned_url("get_object", ExpiresIn=300, Params={
        "Bucket": BUCKET, "Key": f"{kind}/{name}",
        "ResponseContentDisposition": f'attachment; filename="{name}"'})
