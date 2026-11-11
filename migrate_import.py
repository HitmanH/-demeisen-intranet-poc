"""One-time import of the old Lightsail SQLite DB into MySQL.
Runs at startup when IMPORT_SQLITE_S3_KEY is set; records completion in table `import_log`
so it never runs twice. Copies every row with its original id."""
import os, sqlite3, tempfile, dbconn, storage


def run(conn):
    key = os.environ.get("IMPORT_SQLITE_S3_KEY")
    if not (key and dbconn.MYSQL and storage.BUCKET): return
    conn.execute("create table if not exists import_log(src varchar(255) primary key, at varchar(32), counts text)")
    if conn.execute("select 1 from import_log where src=?", (key,)).fetchone(): return
    if any(conn.execute(f"select count(*) from {t}").fetchone()[0] for t in dbconn.TABLES if t != "users"):
        raise RuntimeError("MySQL already holds data - refusing to import over it")
    tmp = os.path.join(tempfile.mkdtemp(), "import.db")
    storage.s3().download_file(storage.BUCKET, key, tmp)
    src = sqlite3.connect(tmp); src.row_factory = sqlite3.Row
    counts = {}
    conn.execute("delete from users")          # only the auto-seeded defaults; real ones come from the import
    for t in dbconn.TABLES:
        rows = src.execute(f"select * from {t}").fetchall()
        for r in rows:
            cols = r.keys()
            conn.execute(f"insert into {t}({','.join(cols)}) values({','.join('?' * len(cols))})", tuple(r))
        counts[t] = len(rows)
    from datetime import datetime
    conn.execute("insert into import_log values(?,?,?)", (key, datetime.now().isoformat(timespec="seconds"), repr(counts)))
    conn.commit(); src.close()
    print("IMPORTED", counts, flush=True)
