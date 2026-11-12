"""Tiny DB layer: SQLite locally, MySQL when DB_HOST is set. Keeps the app's sqlite-style SQL
(? placeholders, `insert or ignore`, `insert or replace`) and translates it for MySQL."""
import os, re, sqlite3

MYSQL = bool(os.environ.get("DB_HOST"))


class Row(dict):
    """Row usable as r["col"], r[0] and dict(r)."""
    def __getitem__(self, k):
        if isinstance(k, int): return list(self.values())[k]
        return super().__getitem__(k)


SQLITE_SCHEMA = """
create table if not exists users(id integer primary key, name text, email text unique,
  pw text, admin int default 0, active int default 1);
create table if not exists ndas(id integer primary key, ref text, counterparty text, cp_details text,
  cp_type text, purpose text, ci_scope text, duration text, conf_period text, governing_law text,
  changes text, effective_date text, our_signatory text, cp_signatory text, cp_signatory_title text,
  cp_email text, status text, docx text, pdf text, signed text, created_by int, created text,
  dispute text, our_title text, our_email text);
create table if not exists issues(id integer primary key, ref text, prospect text, src_name text,
  docx text, pdf text, sha text, notes text, created_by int, created text);
create table if not exists memos(id integer primary key, ref text, title text, amount real,
  currency text, supplier text, body text, attachment text, status text, created_by int, created text);
create table if not exists signoffs(id integer primary key, memo_id int, user_id int, decision text,
  comment text, at text, ip text, unique(memo_id, user_id));
create table if not exists audit(id integer primary key, at text, user_id int, entity text,
  entity_id int, action text, detail text);
"""

MYSQL_SCHEMA = """
create table if not exists users(id int auto_increment primary key, name varchar(255), email varchar(255) unique,
  pw text, admin int default 0, active int default 1);
create table if not exists ndas(id int auto_increment primary key, ref varchar(64), counterparty text, cp_details text,
  cp_type text, purpose text, ci_scope text, duration text, conf_period text, governing_law text,
  changes text, effective_date text, our_signatory text, cp_signatory text, cp_signatory_title text,
  cp_email text, status varchar(64), docx text, pdf text, signed text, created_by int, created varchar(32),
  dispute text, our_title text, our_email text, index(ref));
create table if not exists issues(id int auto_increment primary key, ref varchar(64), prospect text, src_name text,
  docx text, pdf text, sha varchar(64), notes text, created_by int, created varchar(32), index(ref), index(sha));
create table if not exists memos(id int auto_increment primary key, ref varchar(64), title text, amount double,
  currency text, supplier text, body mediumtext, attachment text, status varchar(64), created_by int, created varchar(32));
create table if not exists signoffs(id int auto_increment primary key, memo_id int, user_id int, decision text,
  comment text, at varchar(32), ip varchar(64), unique(memo_id, user_id));
create table if not exists audit(id int auto_increment primary key, at varchar(32), user_id int, entity varchar(32),
  entity_id int, action text, detail text, index(entity, entity_id));
"""
TABLES = ["users", "ndas", "issues", "memos", "signoffs", "audit"]


def _mysql_sql(sql):
    sql = re.sub(r"(?i)\binsert\s+or\s+ignore\b", "insert ignore", sql)
    sql = re.sub(r"(?i)\binsert\s+or\s+replace\b", "replace", sql)
    return sql.replace("%", "%%").replace("?", "%s")


class _Cur:
    def __init__(self, cur): self._c = cur; self.lastrowid = cur.lastrowid
    def _row(self, r):
        if r is None: return None
        return Row(r) if isinstance(r, dict) else Row(zip(r.keys(), tuple(r)))
    def fetchone(self): return self._row(self._c.fetchone())
    def fetchall(self): return [self._row(r) for r in self._c.fetchall()]
    def __iter__(self): return iter(self.fetchall())


class Conn:
    def __init__(self, path=None):
        if MYSQL:
            import pymysql
            self._c = pymysql.connect(host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", 3306)),
                                      user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
                                      database=os.environ.get("DB_NAME", "intranet"), charset="utf8mb4",
                                      cursorclass=pymysql.cursors.DictCursor, autocommit=False,
                                      ssl={"ca": os.environ["DB_SSL_CA"]} if os.environ.get("DB_SSL_CA") else None)
        else:
            self._c = sqlite3.connect(path); self._c.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if MYSQL:
            cur = self._c.cursor(); cur.execute(_mysql_sql(sql), tuple(params)); return _Cur(cur)
        return _Cur(self._c.execute(sql, params))

    def executescript(self, script):
        for stmt in [s.strip() for s in script.split(";") if s.strip()]:
            self._c.cursor().execute(stmt) if MYSQL else self._c.execute(stmt)

    def commit(self): self._c.commit()
    def close(self): self._c.close()

    def ensure_schema(self):
        self.executescript(MYSQL_SCHEMA if MYSQL else SQLITE_SCHEMA)
        if not MYSQL:   # old sqlite files predate these columns
            cols = [r[1] for r in self._c.execute("pragma table_info(ndas)")]
            for col in ("dispute", "our_title", "our_email"):
                if col not in cols: self._c.execute(f"alter table ndas add column {col} text")
        self.commit()


def bootstrap():
    """Create the intranet database + its own least-privilege user on a shared MySQL server.
    Only runs when DB_ADMIN_USER/DB_ADMIN_PASSWORD are provided (first deploy); idempotent."""
    if not (MYSQL and os.environ.get("DB_ADMIN_USER")): return
    import pymysql
    name, user, pw = os.environ.get("DB_NAME", "intranet"), os.environ["DB_USER"], os.environ["DB_PASSWORD"]
    assert re.fullmatch(r"\w+", name) and re.fullmatch(r"\w+", user)
    c = pymysql.connect(host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", 3306)),
                        user=os.environ["DB_ADMIN_USER"], password=os.environ["DB_ADMIN_PASSWORD"], charset="utf8mb4",
                        ssl={"ca": os.environ["DB_SSL_CA"]} if os.environ.get("DB_SSL_CA") else None)
    with c.cursor() as k:
        k.execute(f"create database if not exists `{name}` character set utf8mb4 collate utf8mb4_unicode_ci")
        k.execute(f"create user if not exists '{user}'@'%%' identified by %s", (pw,))
        k.execute(f"alter user '{user}'@'%%' identified by %s", (pw,))
        k.execute(f"grant all privileges on `{name}`.* to '{user}'@'%'")
    c.commit(); c.close(); print("BOOTSTRAP ok", flush=True)
