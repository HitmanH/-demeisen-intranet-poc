"""AWS Lambda entry point (Function URL -> Flask), plus admin tasks that can only be triggered by a direct
`lambda.invoke` with AWS credentials (Function URL requests never carry a top-level "admin_task" key)."""
from apig_wsgi import make_lambda_handler
from app import app, db

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
        # the app role can't delete S3 objects (by design): remove these with admin credentials
        info["s3_keys_to_delete"] = [("uploads/" if f == n["signed"] else "generated/") + f for f in info["files"]]
        return {"deleted": info}


def handler(event, context):
    task = event.get("admin_task") if isinstance(event, dict) else None
    if task == "delete_nda": return _delete_nda(event["ref"], event.get("confirm"))
    if task == "list_ndas":
        with app.app_context():
            return [dict(r) for r in db().execute("select id, ref, counterparty, status, created from ndas")]
    return _web(event, context)
