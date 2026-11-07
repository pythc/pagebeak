"""Device-local, rebuildable document index. No model calls and no implicit upload.

Only the local stdio connector imports this module. Cloud APIs never open client paths.
Original text is kept separately from the CJK token stream used for FTS.
"""

import contextlib
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import UUID, uuid4

from akb.mcp_upload import signature
from akb.workbuddy_entry import private_directory

EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".html",
    ".htm",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".doc",
    ".xls",
    ".ppt",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
}
EXCLUDED = {
    "library",
    "applications",
    "node_modules",
    "vendor",
    "venv",
    "__pycache__",
    "secrets",
    "credentials",
    "缓存",
    "回收站",
    "$recycle.bin",
}
MAX_DOCUMENTS, MAX_PARSE_BYTES = 10000, 128 * 1024**2
MAX_TEXT = 500000
CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]+")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def tokenize(text):
    return CJK.sub(lambda m: " " + " ".join(m[0]) + " ", text)


def fts_query(text):
    # Treat input as text, never as FTS operators or SQL.
    tokens = re.findall(r"[\w\u3400-\u9fff]+", text, re.UNICODE)
    return " AND ".join('"' + tokenize(t).strip() + '"' for t in tokens[:20])


@contextlib.contextmanager
def open_original(root, relative):
    """Walk each canonical path component with dirfd + O_NOFOLLOW (including parents)."""
    relative = Path(relative)
    if relative.is_absolute() or not relative.parts or any(p in {".", ".."} for p in relative.parts):
        raise ValueError("本机文件相对路径无效")
    full = Path(root) / relative
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in full.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        fd = os.open(full.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(fd)
            raise ValueError("仅处理普通文件；符号链接、硬链接和设备文件不纳入")
        with os.fdopen(fd, "rb") as stream:
            yield stream, info
    finally:
        os.close(descriptor)


class LocalIndex:
    def __init__(self, knowledge_base_id, root=None):
        self.kb = str(UUID(knowledge_base_id))
        base = Path(root or os.environ.get("AKB_LOCAL_INDEX_ROOT", Path.home() / ".zhixu" / "local"))
        if not base.is_absolute():
            raise ValueError("本地索引目录必须为绝对路径")
        # Existing symlink ancestors are not accepted as an alternate credential/cache store.
        if base != base.resolve():
            raise ValueError("本地索引目录不能包含符号链接，请使用规范路径")
        private_directory(base)
        self.directory = base / self.kb
        private_directory(self.directory)
        self.path = self.directory / "index.sqlite"
        if self.path.is_symlink():
            raise ValueError("本地索引数据库不能为链接")
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents(
              id TEXT PRIMARY KEY,root TEXT NOT NULL,path TEXT NOT NULL,signature TEXT,sha256 TEXT,
              size INTEGER NOT NULL,status TEXT NOT NULL,coverage TEXT,warning TEXT,summary TEXT,
              blocks TEXT NOT NULL DEFAULT '[]',seen TEXT,source_id TEXT,imported_hash TEXT,
              UNIQUE(root,path));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,status TEXT NOT NULL,pid INTEGER,
              revision INTEGER NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,document_id TEXT,
              operation TEXT NOT NULL,before_hash TEXT,after_hash TEXT,path TEXT,created REAL NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS fulltext USING fts5(document_id UNINDEXED,ordinal UNINDEXED,
              title,body,tokenize='unicode61');
            CREATE TABLE IF NOT EXISTS imports(request_id TEXT PRIMARY KEY,document_id TEXT NOT NULL,
              sha256 TEXT NOT NULL,size INTEGER NOT NULL,scope TEXT NOT NULL,result TEXT);
            """)
            self.put(db, "device_id", self.get(db, "device_id", str(uuid4())))
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def get(db, key, default=None):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put(db, key, value):
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, encoded(value)))

    def config(self, db):
        return self.get(db, "config", {"revision": 0, "enabled": False, "roots": [], "share_catalog": False})

    def configure(
        self,
        *,
        enabled,
        roots=None,
        all_documents=False,
        auto_import=False,
        share_catalog=False,
        import_budget_bytes=1024**3,
        allow_ocr=False,
        exclude=None,
    ):
        if type(import_budget_bytes) is not int or not 0 <= import_budget_bytes <= 20 * 1024**3:
            raise ValueError("自动收录总预算应为 0 至 20 GiB")
        selected = [str(Path.home())] if all_documents else roots or []
        if enabled and not selected:
            raise ValueError("请提供用户选定的目录，或经用户同意使用 all_documents 检索个人主目录中的文档")
        if len(selected) > 20 or len(exclude or []) > 50:
            raise ValueError("目录或排除规则过多")
        canonical = []
        for value in selected if enabled else []:
            path = Path(value).expanduser()
            if not path.is_absolute() or not path.is_dir() or path.is_symlink():
                raise ValueError("请选择存在的普通目录，不能使用符号链接")
            path = path.resolve()
            if path in {Path("/"), Path("/Users"), Path("/System"), Path("/Library"), Path("/Volumes")}:
                raise ValueError("不接受系统根目录；外置磁盘请选其中的具体资料目录")
            if any(part.startswith(".") or part.casefold() in EXCLUDED for part in path.parts[1:]):
                raise ValueError("不能将隐藏目录、凭据目录或系统目录作为扫描根目录")
            if not any(path == p or p in path.parents for p in canonical):
                canonical = [p for p in canonical if path not in p.parents] + [path]
        with self.db() as db:
            old = self.config(db)
            conf = {
                "revision": old["revision"] + 1,
                "enabled": enabled,
                "roots": [str(p) for p in canonical],
                "all_documents": all_documents,
                "auto_import": auto_import and enabled,
                "share_catalog": share_catalog and enabled,
                "import_budget_bytes": import_budget_bytes,
                "allow_ocr": allow_ocr,
                "exclude": exclude or [],
            }
            self.put(db, "config", conf)
            # Revoking/removing roots immediately removes text from retrieval, not just next scan.
            allowed = set(conf["roots"])
            for row in db.execute("SELECT id,root,path FROM documents").fetchall():
                if row["root"] not in allowed or self.excluded(Path(row["path"]), conf["exclude"]):
                    db.execute("DELETE FROM fulltext WHERE document_id=?", (row["id"],))
                    db.execute("DELETE FROM documents WHERE id=?", (row["id"],))
                    db.execute(
                        "UPDATE changes SET path='已移出授权范围',before_hash=NULL,after_hash=NULL WHERE document_id=?",
                        (row["id"],),
                    )
        return {
            **conf,
            "excluded_directories": sorted(EXCLUDED),
            "hidden_files_excluded": True,
            "extensions": sorted(EXTENSIONS),
            "max_documents": MAX_DOCUMENTS,
            "max_parse_bytes": MAX_PARSE_BYTES,
            "model_calls_for_indexing": 0,
            "notice": "全部文档指个人主目录内受支持的非隐藏文档；系统目录、凭据和链接排除。"
            "其他磁盘需另选目录；本机索引不上传原件。交给 WorkBuddy 的片段会由其模型处理。",
        }

    def start(self, expected_revision, *, spawn=True):
        with self.db() as db:
            conf = self.config(db)
            if not conf["enabled"] or conf["revision"] != expected_revision:
                raise ValueError("本地检索未启用或范围已变化，请重新读取设置")
            active = db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running','syncing') ORDER BY created DESC"
            ).fetchall()
            for job in active:
                if job["revision"] != expected_revision:
                    db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job["id"],))
                    continue
                if self.alive(job):
                    return {"job_id": job["id"], "status": job["status"], "reused": True}
                db.execute("UPDATE jobs SET status='interrupted' WHERE id=?", (job["id"],))
            jid, now = str(uuid4()), time.time()
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                (jid, "queued", 0, expected_revision, now, now, "{}"),
            )
        if spawn:
            env = {
                **os.environ,
                "AKB_LOCAL_INDEX_ROOT": str(self.directory.parent),
                "AKB_EXPECTED_KB_ID": self.kb,
            }
            child = subprocess.Popen(
                [sys.executable, "-m", "akb.local_scan", jid],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            with self.db() as db:
                db.execute("UPDATE jobs SET pid=? WHERE id=?", (child.pid, jid))
        return {"job_id": jid, "status": "queued", "reused": False}

    @staticmethod
    def alive(job):
        if time.time() - job["updated"] > 180:
            return False
        if not job["pid"]:
            return time.time() - job["created"] < 10
        try:
            os.kill(job["pid"], 0)
            return True
        except ProcessLookupError:
            return False

    def status(self, job_id=None):
        with self.db() as db:
            conf = self.config(db)
            job = (
                db.execute("SELECT * FROM jobs WHERE id=?", (str(UUID(job_id)),)).fetchone()
                if job_id
                else db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 1").fetchone()
            )
            if job_id and not job:
                raise ValueError("本库本机不存在该扫描任务")
            if job and job["status"] in {"running", "queued", "syncing"} and not self.alive(job):
                db.execute("UPDATE jobs SET status='interrupted' WHERE id=?", (job["id"],))
                job = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            counts = dict(db.execute("SELECT status,count(*) FROM documents GROUP BY status").fetchall())
            imports = db.execute(
                "SELECT coalesce(sum(size),0),coalesce(sum(CASE WHEN result IS NULL THEN size ELSE 0 END),0) FROM imports"
            ).fetchone()
            return {
                "knowledge_base_id": self.kb,
                "device_id": self.get(db, "device_id"),
                "configuration": conf,
                "counts": counts,
                "index_bytes": self.path.stat().st_size,
                "import_budget": {
                    "used_or_reserved_bytes": imports[0],
                    "pending_bytes": imports[1],
                    "available_bytes": max(0, conf.get("import_budget_bytes", 0) - imports[0]),
                },
                "job": {"id": job["id"], "status": job["status"], **json.loads(job["result"])}
                if job
                else None,
                "catalog_sync": self.get(db, "catalog_sync"),
                "background_mode": "扫描子进程完成当前任务；新会话主动增量检查，无常驻监听承诺",
            }

    def event(self, db, doc, operation, after=None, path=None):
        db.execute(
            "INSERT INTO changes(document_id,operation,before_hash,after_hash,path,created) VALUES(?,?,?,?,?,?)",
            (doc["id"], operation, doc["sha256"], after, path or doc["path"], time.time()),
        )

    @staticmethod
    def excluded(relative, patterns):
        return any(
            p.startswith(".") or p.startswith("~$") or p.casefold() in EXCLUDED for p in relative.parts
        ) or any(fnmatch.fnmatch(relative.as_posix(), pattern) for pattern in patterns)

    def scan(self, job_id, api=None):
        with self.db() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            conf = self.config(db)
            if not job or conf["revision"] != job["revision"] or not conf["enabled"]:
                if job:
                    db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job_id,))
                return
            db.execute(
                "UPDATE jobs SET status='running',pid=?,updated=? WHERE id=?",
                (os.getpid(), time.time(), job_id),
            )
        result = {
            "visited": 0,
            "parsed": 0,
            "unchanged": 0,
            "errors": 0,
            "excluded": 0,
            "removed": 0,
            "renamed": 0,
            "deferred": 0,
            "model_calls": 0,
            "truncated": False,
        }
        started = time.monotonic()
        try:
            for root in conf["roots"]:
                walk_errors = []
                online = Path(root).is_dir() and not Path(root).is_symlink()
                for folder, dirs, names in (
                    os.walk(root, followlinks=False, onerror=walk_errors.append) if online else []
                ):
                    relative_dir = Path(folder).relative_to(root)
                    dirs[:] = sorted(
                        d
                        for d in dirs
                        if not self.excluded(relative_dir / d, conf["exclude"])
                        and not (Path(folder) / d).is_symlink()
                    )
                    for name in sorted(names):
                        rel = relative_dir / name
                        if self.excluded(rel, conf["exclude"]) or rel.suffix.lower() not in EXTENSIONS:
                            result["excluded"] += 1
                            continue
                        if result["visited"] >= MAX_DOCUMENTS:
                            result["truncated"] = True
                            break
                        with self.db() as db:
                            if self.config(db)["revision"] != conf["revision"]:
                                raise ValueError("扫描范围已更新，旧扫描已取消")
                        result["visited"] += 1
                        try:
                            self.index_one(root, rel.as_posix(), job_id, conf, result)
                        except (OSError, ValueError) as exc:
                            result["errors"] += 1
                            with self.db() as db:
                                old = db.execute(
                                    "SELECT * FROM documents WHERE root=? AND path=?", (root, rel.as_posix())
                                ).fetchone()
                                if old:
                                    state = "changed" if isinstance(exc, ValueError) else "offline"
                                    self.event(db, old, state)
                                    db.execute(
                                        "UPDATE documents SET status=?,seen=?,sha256=?,warning=? WHERE id=?",
                                        (
                                            state,
                                            job_id,
                                            None if state == "changed" else old["sha256"],
                                            "本机原件读取未完成，请检查权限或等待文件写入结束后重扫",
                                            old["id"],
                                        ),
                                    )
                                    db.execute("DELETE FROM fulltext WHERE document_id=?", (old["id"],))
                        with self.db() as db:
                            db.execute(
                                "UPDATE jobs SET result=?,updated=? WHERE id=?",
                                (encoded(result), time.time(), job_id),
                            )
                    if result["truncated"]:
                        break
                # Never interpret an inaccessible or incomplete traversal as deletion.
                if not result["truncated"]:
                    with self.db() as db:
                        if self.config(db)["revision"] != conf["revision"]:
                            raise ValueError("扫描范围已更新")
                        for row in db.execute(
                            "SELECT * FROM documents WHERE root=? AND (seen IS NULL OR seen!=?)",
                            (root, job_id),
                        ).fetchall():
                            state = "offline" if not online or walk_errors else "missing"
                            if row["status"] != state:
                                self.event(db, row, state)
                            db.execute("UPDATE documents SET status=? WHERE id=?", (state, row["id"]))
                            db.execute("DELETE FROM fulltext WHERE document_id=?", (row["id"],))
                            result["removed"] += int(state == "missing")
                if not online or walk_errors:
                    result["errors"] += 1
                if result["truncated"]:
                    break
            state = "partial" if result["errors"] or result["truncated"] else "completed"
        except Exception as exc:
            state = "cancelled" if isinstance(exc, ValueError) else "failed"
            result["error"] = "扫描已停止，可在当前范围重新开始；已完成的索引可复用"
        result["seconds"] = round(time.monotonic() - started, 3)
        if api and state not in {"cancelled", "failed"}:
            with self.db() as db:
                db.execute("UPDATE jobs SET status='syncing',updated=? WHERE id=?", (time.time(), job_id))
            result["catalog_sync"] = self.publish_catalog(api)
        with self.db() as db:
            db.execute(
                "UPDATE jobs SET status=?,result=?,updated=? WHERE id=?",
                (state, encoded(result), time.time(), job_id),
            )

    def index_one(self, root, relative, job_id, conf, result):
        with open_original(root, relative) as (stream, info):
            sig = encoded([*signature(info), "parser-1", conf["allow_ocr"]])
            with self.db() as db:
                old = db.execute(
                    "SELECT * FROM documents WHERE root=? AND path=?", (root, relative)
                ).fetchone()
                if old and old["signature"] == sig and old["status"] in {"indexed", "deferred"}:
                    db.execute("UPDATE documents SET seen=? WHERE id=?", (job_id, old["id"]))
                    result["unchanged"] += 1
                    return
            if info.st_size > MAX_PARSE_BYTES or info.st_size == 0:
                sha, parsed, status, warning = (
                    None,
                    None,
                    "deferred",
                    "空文件或超过 128 MiB 本机解析预算；可按需上传原件",
                )
            else:
                with tempfile.TemporaryDirectory(dir=self.directory) as temp:
                    snapshot = Path(temp) / "original"
                    digest = hashlib.sha256()
                    with snapshot.open("wb") as target:
                        while chunk := stream.read(1024**2):
                            digest.update(chunk)
                            target.write(chunk)
                    if signature(os.fstat(stream.fileno())) != signature(info):
                        raise ValueError("文件在读取期间变化")
                    sha = digest.hexdigest()
                    parsed = None
                    try:
                        output = subprocess.run(
                            [
                                sys.executable,
                                "-m",
                                "akb.local_parse",
                                str(snapshot),
                                relative,
                                "1" if conf["allow_ocr"] else "0",
                            ],
                            timeout=60,
                            capture_output=True,
                        )
                        if output.returncode:
                            raise ValueError("解析进程失败")
                        parsed = json.loads(output.stdout)
                        status, warning = parsed["status"], parsed.get("warning")
                    except (subprocess.TimeoutExpired, ValueError):
                        status, warning = "failed", "解析超时或失败，索引未完成；可按需单独处理"
            with self.db() as db:
                if self.config(db)["revision"] != conf["revision"]:
                    raise ValueError("范围已撤销")
                # Recognize a rename only when the previous path disappeared; duplicate copies stay separate.
                if not old and sha:
                    candidates = db.execute(
                        "SELECT * FROM documents WHERE root=? AND sha256=?", (root, sha)
                    ).fetchall()
                    candidates = [
                        r
                        for r in candidates
                        if json.loads(r["signature"] or "[]")[:2] == list(signature(info))[:2]
                        and not (Path(root) / r["path"]).exists()
                    ]
                    if len(candidates) == 1:
                        old = candidates[0]
                        db.execute("UPDATE documents SET path=? WHERE id=?", (relative, old["id"]))
                        self.event(db, old, "renamed", sha, relative)
                        result["renamed"] += 1
                did = old["id"] if old else str(uuid4())
                blocks = (parsed or {}).get("blocks", [])
                coverage = (parsed or {}).get("coverage", "none")
                summary = " ".join(b["text"] for b in blocks)[:240]
                if not old:
                    db.execute(
                        "INSERT INTO documents(id,root,path,size,status) VALUES(?,?,?,?,?)",
                        (did, root, relative, info.st_size, status),
                    )
                if not old or old["sha256"] != sha or old["status"] != status:
                    self.event(
                        db,
                        {"id": did, "sha256": old["sha256"] if old else None, "path": relative},
                        "modified" if old else "added",
                        sha,
                    )
                db.execute(
                    "UPDATE documents SET signature=?,sha256=?,size=?,status=?,coverage=?,warning=?,summary=?,blocks=?,seen=? WHERE id=?",
                    (
                        sig,
                        sha,
                        info.st_size,
                        status,
                        coverage,
                        warning,
                        summary,
                        encoded(blocks),
                        job_id,
                        did,
                    ),
                )
                db.execute("DELETE FROM fulltext WHERE document_id=?", (did,))
                for i, block in enumerate(blocks if status == "indexed" else []):
                    db.execute(
                        "INSERT INTO fulltext VALUES(?,?,?,?)",
                        (did, i, tokenize(relative), tokenize(block["text"])),
                    )
            result["parsed"] += int(status == "indexed")
            result["deferred"] += int(status == "deferred")
            result["errors"] += int(status == "failed")

    def enabled(self, db):
        conf = self.config(db)
        if not conf["enabled"]:
            raise ValueError("用户尚未启用本机资料检索")
        return conf

    @staticmethod
    def metadata(row):
        return {
            k: row[k] for k in ("id", "path", "sha256", "size", "status", "coverage", "warning", "source_id")
        } | {
            "root_label": Path(row["root"]).name,
            "root_id": hashlib.sha256(row["root"].encode()).hexdigest()[:12],
            "warning": (
                row["warning"][:497] + "..."
                if row["warning"] and len(row["warning"]) > 500
                else row["warning"]
            ),
            "imported_current": bool(row["source_id"] and row["sha256"] == row["imported_hash"]),
        }

    def catalog(self, offset=0, limit=50):
        with self.db() as db:
            self.enabled(db)
            total = db.execute("SELECT count(*) FROM documents").fetchone()[0]
            rows = db.execute(
                "SELECT * FROM documents ORDER BY root,path LIMIT ? OFFSET ?",
                (min(100, max(1, limit)), max(0, offset)),
            ).fetchall()
            return {
                "device_id": self.get(db, "device_id"),
                "total": total,
                "has_more": offset + len(rows) < total,
                "items": [
                    self.metadata(r) | {"excerpt": r["summary"], "excerpt_kind": "原文开头，非模型摘要"}
                    for r in rows
                ],
            }

    def changes(self, after=0, limit=50):
        after, limit = max(0, after), min(100, max(1, limit))
        with self.db() as db:
            self.enabled(db)
            rows = db.execute(
                "SELECT * FROM changes WHERE seq>? ORDER BY seq LIMIT ?", (after, limit + 1)
            ).fetchall()
            return {
                "items": [dict(r) for r in rows[:limit]],
                "has_more": len(rows) > limit,
                "cursor": rows[min(len(rows), limit) - 1]["seq"] if rows else after,
            }

    def search(self, query, limit=8):
        if not query.strip() or len(query) > 500:
            raise ValueError("检索词应为 1 至 500 字符")
        limit = min(20, max(1, limit))
        expression = fts_query(query)
        with self.db() as db:
            self.enabled(db)
            rows = (
                db.execute(
                    """SELECT documents.*,fulltext.ordinal FROM fulltext JOIN documents ON documents.id=fulltext.document_id
                WHERE fulltext MATCH ? AND documents.status='indexed' ORDER BY bm25(fulltext,0,0,3,1) LIMIT ?""",
                    (expression, limit * 3),
                ).fetchall()
                if expression
                else []
            )
            hits, seen = [], set()
            for row in rows:
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                block = json.loads(row["blocks"])[int(row["ordinal"])]
                text = block["text"]
                at = text.casefold().find(query.casefold())
                start = max(0, at - 120) if at >= 0 else 0
                hits.append(
                    self.metadata(row)
                    | {
                        "ordinal": row["ordinal"],
                        "locator": block["locator"],
                        "snippet": text[start : start + 800],
                        "verified_at_read": False,
                    }
                )
                if len(hits) == limit:
                    break
            return {
                "items": hits,
                "origin": "local",
                "next": "read_local_document 重新核验原件后阅读；正式填写前按授权收录原件",
            }

    @contextlib.contextmanager
    def verified(self, document_id, expected_hash):
        with self.db() as db:
            conf = self.enabled(db)
            row = db.execute("SELECT * FROM documents WHERE id=?", (str(UUID(document_id)),)).fetchone()
            if (
                not row
                or row["root"] not in conf["roots"]
                or not row["sha256"]
                or row["sha256"] != expected_hash
            ):
                raise ValueError("本机资料不存在、范围已撤销或索引版本不符，请重新扫描")
        with open_original(row["root"], row["path"]) as (stream, info):
            digest = hashlib.sha256()
            while chunk := stream.read(1024**2):
                digest.update(chunk)
            if digest.hexdigest() != expected_hash or signature(info) != signature(os.fstat(stream.fileno())):
                with self.db() as db:
                    self.event(db, row, "changed_since_scan", digest.hexdigest())
                    db.execute("UPDATE documents SET status='changed' WHERE id=?", (row["id"],))
                    db.execute("DELETE FROM fulltext WHERE document_id=?", (row["id"],))
                raise ValueError("本地原件已变化，请增量扫描后重新核对")
            with self.db() as db:
                if self.config(db)["revision"] != conf["revision"]:
                    raise ValueError("范围在读取期间变化，已停止")
            stream.seek(0)
            yield row, stream, info, conf

    def read(self, document_id, expected_hash, offset=0, limit=8):
        with self.verified(document_id, expected_hash) as (row, _, _, _):
            if row["status"] not in {"indexed", "offline", "missing"}:
                raise ValueError("该文档尚无可阅读的正文索引")
            blocks = json.loads(row["blocks"])
            offset, limit = max(0, offset), min(20, max(1, limit))
            return self.metadata(row) | {
                "blocks": [
                    {"ordinal": i, **b} for i, b in enumerate(blocks[offset : offset + limit], offset)
                ],
                "has_more": offset + limit < len(blocks),
                "total": len(blocks),
                "verified_hash": expected_hash,
                "formal_evidence": False,
            }

    def publish_catalog(self, api):
        import fcntl

        fd = os.open(self.directory / "publish.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._publish_catalog(api)

    def _publish_catalog(self, api):
        with self.db() as db:
            conf = self.config(db)
            share = conf["enabled"] and conf["share_catalog"]
            previously = self.get(db, "catalog_shared", False)
            observations = [
                {
                    "document_id": r["id"],
                    "source_id": r["source_id"],
                    "sha256": r["imported_hash"],
                    "current_hash": r["sha256"],
                    "status": r["status"],
                }
                for r in db.execute("SELECT * FROM documents WHERE source_id IS NOT NULL")
            ]
            rows = (
                db.execute("SELECT * FROM documents WHERE status!='missing' ORDER BY root,path").fetchall()
                if share
                else []
            )
            # Deliberately exclude raw text, excerpts, absolute paths, and configuration.
            items = [self.metadata(r) for r in rows]
            device = self.get(db, "device_id")
            body = {
                "device_id": device,
                "snapshot_id": str(uuid4()),
                "items": items,
                "expected_revision": self.get(db, "remote_revision", 0),
            }
        try:
            # Only hashes/IDs of originals previously authorized for upload are sent here.
            for offset in range(0, len(observations), 1000):
                api(
                    "POST",
                    "/v2/local/observe",
                    {"device_id": device, "items": observations[offset : offset + 1000]},
                )
            if not share and not previously:
                return {"status": "local_only", "observed_imported_sources": len(observations)}
            if len(items) > MAX_DOCUMENTS:
                result = {
                    "status": "pending",
                    "message": "当前本机清单超出单次共享上限，请缩小授权范围后重试；云端保留上次清单",
                }
                with self.db() as db:
                    self.put(db, "catalog_sync", result)
                return result
            for attempt in range(2):
                try:
                    response = api("POST", "/v2/local/catalog", body)
                    break
                except ValueError as exc:
                    if getattr(exc, "status_code", None) != 409 or attempt:
                        raise
                    remote = api("GET", f"/v2/local/catalog/{device}?limit=1")
                    body["expected_revision"] = remote["revision"]
            with self.db() as db:
                self.put(db, "remote_revision", response["revision"])
                self.put(db, "catalog_shared", share)
                self.put(db, "catalog_sync", {"status": "synced", "items": len(items)})
            return {"status": "synced", "items": len(items)}
        except (ValueError, OSError):
            result = {"status": "pending", "message": "本地索引已保留；共享目录同步未成功，下次重试"}
            with self.db() as db:
                self.put(db, "catalog_sync", result)
            return result

    def import_document(
        self, api, document_id, expected_hash, request_id, subject_scope="reference", user_authorized=False
    ):
        from akb.mcp_upload import transfer

        request_id = str(UUID(request_id))
        if subject_scope not in {"self", "reference"}:
            raise ValueError("资料归属只支持 self 或 reference")
        # Serialize imports and catalog publication across MCP processes for quota/idempotency.
        import fcntl

        fd = os.open(self.directory / "import.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.verified(document_id, expected_hash) as (row, stream, info, conf):
                if not conf["auto_import"] and not user_authorized:
                    raise ValueError("用户只授权本机检索；请先取得这份原件的收录授权")
                with self.db() as db:
                    prior = db.execute("SELECT * FROM imports WHERE request_id=?", (request_id,)).fetchone()
                    if prior and (prior["document_id"], prior["sha256"], prior["scope"]) != (
                        row["id"],
                        expected_hash,
                        subject_scope,
                    ):
                        raise ValueError("同一 request_id 不能用于不同文件、版本或归属")
                    if prior and prior["result"]:
                        return {**json.loads(prior["result"]), "replayed": True}
                    match = api(
                        "POST", "/v2/local/match", {"sha256": expected_hash, "subject_scope": subject_scope}
                    )
                    if match["source"]:
                        result = {"source": match["source"], "deduplicated": True, "job_id": None}
                        if not prior:
                            db.execute(
                                "INSERT INTO imports VALUES(?,?,?,?,?,NULL)",
                                (request_id, row["id"], expected_hash, 0, subject_scope),
                            )
                    else:
                        used = db.execute("SELECT coalesce(sum(size),0) FROM imports").fetchone()[0]
                        if not prior and used + info.st_size > conf["import_budget_bytes"]:
                            raise ValueError("本机收录累计预算不足，请用户调整预算；云端空间配额仍单独校验")
                        if not prior:
                            db.execute(
                                "INSERT INTO imports VALUES(?,?,?,?,?,NULL)",
                                (request_id, row["id"], expected_hash, info.st_size, subject_scope),
                            )
                        result = None
                if result is None:
                    usage = api("GET", "/v1/storage")
                    if info.st_size > usage["upload_limit_bytes"]:
                        raise ValueError("文件超过云端单文件上限")
                    result = transfer(
                        api,
                        stream,
                        info,
                        Path(row["path"]).name,
                        request_id,
                        subject_scope,
                        None,
                        {"chunk_bytes": min(8 * 1024**2, usage["chunk_bytes"])},
                    )
                with self.db() as db:
                    db.execute(
                        "UPDATE imports SET result=? WHERE request_id=?", (encoded(result), request_id)
                    )
                    db.execute(
                        "UPDATE documents SET source_id=?,imported_hash=? WHERE id=?",
                        (result["source"]["id"], expected_hash, row["id"]),
                    )
                return result | {
                    "local_document_id": row["id"],
                    "original_unchanged": True,
                    "next": "等待解析完成；回读 read_source 获取正式证据，不把本机摘要用于填写校验",
                }
