"""AWS Lambda entry point (Function URL -> Flask), plus admin tasks that can only be triggered by a direct
`lambda.invoke` with AWS credentials (Function URL requests never carry a top-level "admin_task" key)."""
from apig_wsgi import make_lambda_handler
from app import app, db
import storage

_web = make_lambda_handler(app, binary_support=True)


def _delete_nda(ref, confirm):
    with app.app_context():
        n = db().execute("select * from ndas where ref=?", (ref,)).fetchone()
        if not n: return {"error": f"no NDA {ref}"}
        info = {"id": n["id"], "ref": n["ref"], "counterparty": n["counterparty"],
                "files": [f for f in (n["docx"], n["pdf"], n["signed"]) if f],
                "audit_rows": db().execute("select count(*) from audit where entity='nda' and entity_id=?", (n["id"],)).fetchone()[0]}
        if confirm != ref: return {"dry_run": info}
        db().execute("delete from audit where entity='nda' and entity_id=?", (n["id"],))
        db().execute("delete from ndas where id=?", (n["id"],)); db().commit()
        for f in info["files"]:
            kind = "uploads" if f == n["signed"] else "generated"
            if storage.BUCKET: storage.s3().delete_object(Bucket=storage.BUCKET, Key=f"{kind}/{f}")
        return {"deleted": info}


def handler(event, context):
    task = event.get("admin_task") if isinstance(event, dict) else None
    if task == "delete_nda": return _delete_nda(event["ref"], event.get("confirm"))
    if task == "list_ndas":
        with app.app_context():
            return [dict(r) for r in db().execute("select id, ref, counterparty, status, created from ndas")]
    return _web(event, context)
