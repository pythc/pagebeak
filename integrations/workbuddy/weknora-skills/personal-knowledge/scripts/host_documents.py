#!/usr/bin/env python3
"""WorkBuddy Skill helper: local index + exact-file transfer, no daemon or installation.

Uses the host's Python and a bundled pure-Python PDF reader. No model API, OCR,
remote image fetch, or network access except an explicitly supplied scoped ticket.
"""

import argparse
import contextlib
import hashlib
import html.parser
import io
import json
import mimetypes
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

LIMIT = 128 * 1024**2
TEXT_LIMIT = 500000
EXCLUDE = {
    "library",
    "appdata",
    "applications",
    "windows",
    "program files",
    "program files (x86)",
    "node_modules",
    "vendor",
    "venv",
    "__pycache__",
    "secrets",
    "credentials",
    "passwords",
    "keychains",
    "凭据",
    "密钥",
    "密码",
    "$recycle.bin",
}
EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".html",
    ".htm",
    ".docx",
    ".xlsx",
    ".pptx",
    ".pdf",
    ".doc",
    ".xls",
    ".ppt",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".tif",
    ".tiff",
}


def sensitive_name(name):
    """Conservative filename filter; cannot guarantee arbitrary documents contain no secrets."""
    stem = Path(name).stem.casefold()
    return (any(word in stem for word in ('账号密码', '账户密码', '登录密码', '密钥', '凭据', '恢复码'))
            or stem == '密码'
            or re.search(r'(^|[ _.-])(passwords?|credentials?|secrets?|api[_-]?keys?|tokens?|recovery[_-]?codes?)([ _.-]|$)', stem) is not None)


def dump(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def private(path):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError("本地缓存目录无效")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(path, 0o700)


def write_json(path, value):
    if path.is_symlink():
        raise ValueError("不写入符号链接")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dump(value))
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def link(path):
    s = path.lstat()
    return stat.S_ISLNK(s.st_mode) or bool(getattr(s, "st_file_attributes", 0) & 1024)


def signature(s):
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink]


@contextlib.contextmanager
def original(path):
    path = Path(path)
    for parent in [path, *path.parents]:
        if link(parent):
            raise ValueError("不读取符号链接、目录链接或重解析点")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("仅读取普通独立文件")
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0),
    )
    with os.fdopen(fd, "rb") as stream:
        if signature(before) != signature(os.fstat(fd)) or path.resolve() != path:
            raise ValueError("读取路径已发生变化")
        yield stream, before
        if signature(before) != signature(os.fstat(fd)) or signature(before) != signature(path.stat()):
            raise ValueError("读取过程中原件已变化，请重新扫描")


def digest(stream):
    h = hashlib.sha256()
    while chunk := stream.read(1024**2):
        h.update(chunk)
    stream.seek(0)
    return h.hexdigest()


def tokens(text):
    return re.sub(r"[\u3400-\u9fff]+", lambda m: " " + " ".join(m[0]) + " ", text)


class PlainHTML(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, text):
        self.parts.append(text)


def parse(name, data):
    ext = Path(name).suffix.lower()
    if ext in {".txt", ".md", ".csv", ".html", ".htm"}:
        text = data.decode("utf-8-sig")
        if ext in {".html", ".htm"}:
            parser = PlainHTML()
            parser.feed(text)
            text = "\n".join(parser.parts)
        return [{"text": text[:TEXT_LIMIT], "location": "正文"}], "partial" if len(
            text
        ) > TEXT_LIMIT else "text_only"
    if ext == ".pdf":
        sys.path.insert(0, str(Path(__file__).with_name("pypdf.zip")))
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("加密 PDF 需用户提供可读版本")
        blocks = [
            {"text": page.extract_text() or "", "location": f"第{i + 1}页"}
            for i, page in enumerate(reader.pages[:40])
        ]
        partial = len(reader.pages) > 40 or any(not b["text"].strip() for b in blocks)
        return blocks, "partial" if partial else "text_only"
    if ext not in {".docx", ".xlsx", ".pptx"}:
        return [], "server_parse_needed"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if sum(i.file_size for i in z.infolist()) > 64 * 1024**2 or len(z.infolist()) > 10000:
            raise ValueError("Office 解压预算超限，需按需上传处理")

        def xml(path):
            data = z.read(path)
            if len(data) > 16 * 1024**2 or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                raise ValueError("XML 内容超限或含外部定义")
            return ET.fromstring(data)

        if ext == ".docx":
            return [
                {"text": "".join(e.itertext()), "location": f"段落/单元格{i + 1}"}
                for i, e in enumerate(
                    xml("word/document.xml").iter(
                        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
                    )
                )
            ], "text_only"
        if ext == ".pptx":
            names = sorted(
                (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                key=lambda n: int(re.search(r"slide(\d+)", n)[1]),
            )
            return [
                {
                    "text": "\n".join(
                        e.text or ""
                        for e in xml(n).iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t")
                    ),
                    "location": f"幻灯片{i + 1}",
                }
                for i, n in enumerate(names)
            ], "text_only"
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        strings = (
            ["".join(si.itertext()) for si in xml("xl/sharedStrings.xml").findall(ns + "si")]
            if "xl/sharedStrings.xml" in z.namelist()
            else []
        )
        blocks = []
        for n in sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
            for row in xml(n).iter(ns + "row"):
                cells = []
                for cell in row.findall(ns + "c"):
                    v = cell.findtext(ns + "v", "")
                    if cell.attrib.get("t") == "s":
                        v = strings[int(v)]
                    elif cell.attrib.get("t") == "inlineStr":
                        v = "".join(cell.find(ns + "is").itertext())
                    if cell.find(ns + "f") is not None:
                        v += " [公式缓存值，正式计算须核验]"
                    cells.append(cell.attrib.get("r", "") + ": " + v)
                blocks.append({"text": " | ".join(cells), "location": n + " 行" + row.attrib.get("r", "")})
        return blocks, "text_only"


def bounded_parse(name, data):
    # One document cannot stall the entire scan. The host's normal sandbox and
    # permissions are inherited unchanged; no packages are installed at runtime.
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--parse-file", name],
            input=data,
            capture_output=True,
            timeout=18,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [], "parse_timeout"
    if result.returncode:
        return [], "server_parse_needed"
    return json.loads(result.stdout)


def parse_worker(name):
    data = sys.stdin.buffer.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("解析预算超限")
    blocks, coverage = parse(name, data)
    kept = []
    remaining = TEXT_LIMIT
    for block in blocks:
        text = block["text"]
        if len(text) > remaining:
            text = text[:remaining]
            coverage = "partial"
        for start in range(0, len(text), 4000):
            kept.append({"text": text[start : start + 4000], "location": block["location"] + f" 字符{start}"})
        remaining -= len(text)
        if not remaining:
            coverage = "partial"
            break
    print(dump([kept, coverage]))


class Index:
    def __init__(self, library, origin, state=None):
        self.library = str(uuid.UUID(library))
        u = urllib.parse.urlsplit(origin)
        if (
            u.username
            or u.password
            or u.query
            or u.fragment
            or u.path not in {"", "/"}
            or not u.hostname
            or not (
                u.scheme == "https" or u.scheme == "http" and u.hostname in {"127.0.0.1", "localhost", "::1"}
            )
        ):
            raise ValueError("服务地址必须是 HTTPS 根地址，回环测试除外")
        self.origin = origin.rstrip("/")
        self.directory = (
            Path(state).expanduser().resolve()
            if state
            else Path.home()
            / ".zhixu"
            / "host"
            / hashlib.sha256(self.origin.encode()).hexdigest()[:16]
            / self.library
        )
        private(self.directory)
        path = self.directory / "index.sqlite"
        if path.is_symlink():
            raise ValueError("缓存数据库不能是链接")
        self.db = sqlite3.connect(path, timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS docs(id TEXT PRIMARY KEY,root TEXT,path TEXT,sha TEXT,size INTEGER,sig TEXT,status TEXT,coverage TEXT,blocks TEXT,seen TEXT,UNIQUE(root,path));
        CREATE TABLE IF NOT EXISTS imports(doc TEXT,source TEXT,sha TEXT,PRIMARY KEY(doc,source));
        CREATE TABLE IF NOT EXISTS changes(seq INTEGER PRIMARY KEY AUTOINCREMENT,doc TEXT,operation TEXT,before_hash TEXT,after_hash TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(id UNINDEXED,title,body);
        """)
        binding = {"library": self.library, "origin": self.origin}
        for key, value in (("device", str(uuid.uuid4())), ("binding", binding)):
            if self.get(key) is None:
                self.db.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (key, dump(value)))
        if self.get("binding") != binding:
            raise ValueError("缓存属于其他空间")
        self.db.commit()
        if os.name != "nt":
            os.chmod(path, 0o600)

    def get(self, k, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, dump(v)))

    def config(self):
        return self.get("config", {"enabled": False, "roots": [], "share_catalog": False})

    @contextlib.contextmanager
    def scan_lock(self):
        path = self.directory / "scan.lock"
        if path.is_symlink():
            raise ValueError("扫描锁不能是链接")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+b") as lock:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError("同一知识库的本机索引正在更新，请等待当前批次完成") from exc
            yield

    def configure(self, conf):
        with self.scan_lock():
            return self._configure(conf)

    def _configure(self, conf):
        if conf.get("user_authorized") is not True:
            raise ValueError("须先获得用户对所选范围的授权")
        roots = []
        for r in [str(Path.home())] if conf.get("all_documents") else conf.get("roots", []):
            p = Path(r).expanduser()
            if not p.is_absolute() or not p.is_dir() or link(p):
                raise ValueError("选择存在的普通绝对目录")
            p = p.resolve()
            if p.parent == p or p.name.casefold() in EXCLUDE | {"users", "system", "volumes"}:
                raise ValueError("不扫描系统根目录")
            if any(q.startswith(".") or q.casefold() in EXCLUDE for q in p.parts):
                raise ValueError("不扫描隐藏、系统或凭据目录")
            if not any(p.is_relative_to(Path(existing)) for existing in roots):
                roots = [existing for existing in roots if not Path(existing).is_relative_to(p)]
                roots.append(str(p))
        if conf.get("enabled") and not roots:
            raise ValueError("未指定授权目录")
        if len(roots) > 20:
            raise ValueError("最多20个目录")
        previous = self.config()
        enabled = bool(conf.get("enabled"))
        same_scope = enabled and previous["enabled"] and roots == previous["roots"]
        value = {
            "enabled": enabled,
            "roots": roots,
            "share_catalog": bool(conf.get("share_catalog")),
            "auto_import": bool(conf.get("auto_import")),
            "publish_summary": enabled and conf.get(
                "publish_summary", same_scope and previous.get("publish_summary", False)
            ) is True,
        }
        if not same_scope:
            self.db.execute("DELETE FROM meta WHERE key IN ('scan_checkpoint','last_scan')")
        self.put("config", value)
        for row in self.db.execute("SELECT * FROM docs").fetchall():
            if not value["enabled"] or row["root"] not in roots:
                self.db.execute("UPDATE docs SET status='offline',blocks='[]' WHERE id=?", (row["id"],))
                self.db.execute("DELETE FROM fts WHERE id=?", (row["id"],))
        self.db.commit()
        return value | {"device_id": self.get("device"), "ocr": "server_cpu_only"}

    def scoped(self, row):
        conf = self.config()
        if not conf["enabled"] or row["root"] not in conf["roots"]:
            raise ValueError("该目录已撤销授权")
        path = Path(row["root"]) / row["path"]
        if sensitive_name(path.name):
            raise ValueError("疑似凭据文件不纳入本机文档索引")
        return path

    def document(self, id, sha=None):
        row = self.db.execute("SELECT * FROM docs WHERE id=?", (str(uuid.UUID(id)),)).fetchone()
        if not row:
            raise ValueError("本机文档不存在")
        with original(self.scoped(row)) as (f, _):
            actual = digest(f)
        if actual != row["sha"] or sha and sha != actual:
            raise ValueError("本机原件已变化，请扫描并同步失效标记后再读取")
        return row

    def scan(self, seconds=25):
        with self.scan_lock():
            return self._scan(seconds)

    def status(self):
        conf = self.config()
        rows = self.db.execute("SELECT root,status,coverage,count(*) FROM docs GROUP BY root,status,coverage")
        counts = {}
        partial = 0
        for root, status, coverage, count in rows:
            if conf["enabled"] and root in conf["roots"]:
                counts[status] = counts.get(status, 0) + count
                if status == "indexed" and coverage == "partial":
                    partial += count
        return {
            "device_id": self.get("device"),
            "config": conf | {"publish_summary": conf.get("publish_summary", False)},
            "status_counts": counts,
            "indexed_partial": partial,
            "last_scan": self.get("last_scan"),
            "resume_pending": self.get("scan_checkpoint") is not None,
        }

    def _scan(self, seconds):
        conf = self.config()
        if not conf["enabled"]:
            raise ValueError("本机资料检索未启用")
        started = time.monotonic()
        checkpoint = self.get("scan_checkpoint") or {}
        seen = checkpoint.get("seen") or str(uuid.uuid4())
        cursor = checkpoint.get("cursor")
        resume = (cursor[0], tuple(cursor[1]), cursor[2]) if cursor else None
        count = checkpoint.get("visited", 0)
        changed = checkpoint.get("changed", 0)
        complete = True
        limit_reason = None
        skipped = checkpoint.get("skipped", 0)
        errors = []
        offline = []
        unchanged = []

        def checkpoint_progress():
            self.db.executemany("UPDATE docs SET seen=? WHERE id=?", unchanged)
            unchanged.clear()
            self.put("scan_checkpoint", {
                "seen": seen, "cursor": cursor, "visited": count,
                "changed": changed, "skipped": skipped,
            })
            self.db.commit()

        for root_index, root in enumerate(conf["roots"]):
            if not Path(root).is_dir() or link(Path(root)):
                offline.append(root)
                with self.db:
                    self.db.execute("UPDATE docs SET status='offline' WHERE root=?", (root,))
                continue
            if resume and root_index < resume[0]:
                continue

            def walk_error(error):
                errors.append(type(error).__name__)

            for parent, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
                parent_parts = Path(parent).relative_to(root).parts
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if not d.startswith(".") and d.casefold() not in EXCLUDE and not link(Path(parent) / d)
                )
                if resume and root_index == resume[0]:
                    resume_dir = resume[1]
                    dirs[:] = [
                        d for d in dirs
                        if parent_parts + (d,) >= resume_dir
                        or resume_dir[:len(parent_parts) + 1] == parent_parts + (d,)
                    ]
                    if parent_parts < resume_dir:
                        continue
                for name in sorted(files):
                    if name.startswith(".") or sensitive_name(name) or Path(name).suffix.lower() not in EXTENSIONS:
                        continue
                    position = (root_index, parent_parts, name)
                    if resume and position <= resume:
                        continue
                    if count >= 10000 or time.monotonic() - started > seconds:
                        complete = False
                        limit_reason = "file_limit" if count >= 10000 else "time_budget"
                        break
                    count += 1
                    cursor = position
                    p = Path(parent) / name
                    rel = p.relative_to(root).as_posix()
                    old = self.db.execute(
                        "SELECT * FROM docs WHERE root=? AND path=?", (root, rel)
                    ).fetchone()
                    sha = None
                    s = None
                    sig = None
                    try:
                        with original(p) as (f, s):
                            sig = dump(signature(s))
                            reusable = old and old["sig"] == sig and old["status"] in {"indexed", "deferred"}
                            if not reusable:
                                sha = digest(f)
                                data = f.read() if s.st_size <= LIMIT else None
                        if reusable:
                            unchanged.append((seen, old["id"]))
                            continue
                        blocks, coverage = (
                            bounded_parse(name, data) if data is not None else ([], "size_limit")
                        )
                        total = 0
                        kept = []
                        for b in blocks:
                            if total + len(b["text"]) > TEXT_LIMIT:
                                coverage = "partial"
                                break
                            kept.append(b)
                            total += len(b["text"])
                        blocks = kept
                        status = "indexed" if any(b["text"].strip() for b in blocks) else "deferred"
                    except (
                        OSError,
                        ValueError,
                        ET.ParseError,
                        zipfile.BadZipFile,
                        IndexError,
                        ImportError,
                    ):
                        if sha is None:
                            skipped += 1
                            if old:
                                self.db.execute(
                                    "UPDATE docs SET status='offline',seen=? WHERE id=?", (seen, old["id"])
                                )
                                self.db.execute("DELETE FROM fts WHERE id=?", (old["id"],))
                            checkpoint_progress()
                            continue
                        sig = None
                        blocks = []
                        coverage = "unreadable"
                        status = "failed"
                    id = old["id"] if old else str(uuid.uuid4())
                    self.db.execute(
                        "INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            id,
                            root,
                            rel,
                            sha,
                            s.st_size if s else 0,
                            sig,
                            status,
                            coverage,
                            dump(blocks),
                            seen,
                        ),
                    )
                    self.db.execute("DELETE FROM fts WHERE id=?", (id,))
                    self.db.execute(
                        "INSERT INTO fts VALUES(?,?,?)",
                        (id, tokens(name), tokens("\n".join(b["text"] for b in blocks))),
                    )
                    if not old or old["sha"] != sha:
                        changed += 1
                        self.db.execute(
                            "INSERT INTO changes(doc,operation,before_hash,after_hash)VALUES(?,?,?,?)",
                            (id, "added" if not old else "changed", old["sha"] if old else None, sha),
                        )
                    checkpoint_progress()
                if not complete:
                    break
            if not complete:
                break
        complete = complete and not errors and not offline
        if errors or offline:
            limit_reason = "directory_unavailable"
        checkpoint_progress()
        if complete:
            placeholders = ",".join("?" for _ in conf["roots"])
            for row in self.db.execute(
                f"SELECT id,status,sha FROM docs WHERE seen!=? AND root IN ({placeholders})",
                (seen, *conf["roots"]),
            ).fetchall():
                if row["status"] != "missing":
                    self.db.execute(
                        "INSERT INTO changes(doc,operation,before_hash,after_hash)VALUES(?,?,?,NULL)",
                        (row["id"], "missing", row["sha"]),
                    )
                self.db.execute("UPDATE docs SET status='missing' WHERE id=?", (row["id"],))
                self.db.execute("DELETE FROM fts WHERE id=?", (row["id"],))
        result = {
            "visited": count,
            "skipped_unreadable_or_links": skipped,
            "directory_errors": len(errors),
            "offline_roots": len(offline),
            "changed": changed,
            "complete": complete,
            "limit_reason": limit_reason,
            "seconds": round(time.monotonic() - started, 2),
            "status_counts": dict(
                self.db.execute("SELECT status,count(*)FROM docs GROUP BY status").fetchall()
            ),
            "indexed_partial": self.status()["indexed_partial"],
            "finished_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 - Python 3.10 host
            "next": "同步已收录原件的版本观察；未解析文件仅支持文件名检索，按需上传后做服务器 CPU OCR",
        }
        if limit_reason != "time_budget":
            self.db.execute("DELETE FROM meta WHERE key='scan_checkpoint'")
        self.put("last_scan", result)
        self.db.commit()
        return result

    def search(self, query, limit=20, titles_only=False):
        terms = re.findall(r"\w+", query, flags=re.UNICODE)[:15]
        if not terms:
            return {"items": []}
        q = " AND ".join('"' + tokens(t).strip() + '"' for t in terms)
        rows = self.db.execute(
            "SELECT docs.* FROM fts JOIN docs ON docs.id=fts.id WHERE fts MATCH ? LIMIT ?", (q, limit * 3)
        ).fetchall()
        items = []
        for row in rows:
            try:
                # Discovery is cheap even for a large unparsed file. Reading or
                # importing below still hashes the actual bytes; changed metadata
                # is never served as a current cached excerpt.
                with original(self.scoped(row)) as (_, current):
                    if dump(signature(current)) != row["sig"]:
                        continue
            except (ValueError, OSError):
                continue
            selected = []
            if not titles_only:
                blocks = json.loads(row["blocks"])
                selected = [b for b in blocks if any(t.casefold() in b["text"].casefold() for t in terms)]
            items.append(
                {
                    "id": row["id"],
                    "path": row["path"],
                    "sha256": row["sha"],
                    "status": row["status"],
                    "coverage": row["coverage"],
                    "snippet": (selected[0]["text"] if selected else "")[:1000],
                    "location": "teacher_local",
                }
            )
            if len(items) >= limit:
                break
        return {
            "items": items,
            "unparsed_count": self.db.execute(
                "SELECT count(*)FROM docs WHERE status IN ('deferred','failed')"
            ).fetchone()[0],
        }

    def read(self, id, sha, offset=0, limit=20):
        row = self.document(id, sha)
        blocks = json.loads(row["blocks"])
        return {
            "id": id,
            "sha256": row["sha"],
            "path": row["path"],
            "coverage": row["coverage"],
            "blocks": blocks[offset : offset + limit],
            "has_more": offset + limit < len(blocks),
            "authority": "local_excerpt_import_before_formal_filling",
        }

    def upload_request(self, id, sha, scope):
        row = self.document(id, sha)
        return {
            "request_id": str(uuid.uuid4()),
            "device_id": self.get("device"),
            "document_id": id,
            "file": {
                "filename": Path(row["path"]).name,
                "size": row["size"],
                "sha256": row["sha"],
                "media_type": mimetypes.guess_type(row["path"])[0] or "application/octet-stream",
                "subject_scope": scope,
            },
        }

    def export_sync(self, revision=None):
        items = []
        observations = []
        for row in self.db.execute("SELECT *FROM docs").fetchall():
            linked = None
            for imp in self.db.execute("SELECT *FROM imports WHERE doc=?", (row["id"],)):
                if imp["sha"] == row["sha"]:
                    linked = imp["source"]
                observations.append(
                    {
                        "document_id": row["id"],
                        "source_id": imp["source"],
                        "sha256": imp["sha"],
                        "current_hash": row["sha"],
                        "status": row["status"],
                    }
                )
            if (
                self.config()["share_catalog"]
                and row["root"] in self.config()["roots"]
                and self.config()["enabled"]
            ):
                items.append(
                    {
                        "id": row["id"],
                        "root_label": Path(row["root"]).name,
                        "root_id": hashlib.sha256(row["root"].encode()).hexdigest()[:12],
                        "path": row["path"],
                        "sha256": row["sha"],
                        "size": row["size"],
                        "status": row["status"],
                        "coverage": row["coverage"],
                        "source_id": linked,
                    }
                )
        revision = self.get("catalog_revision", 0) if revision is None else max(0, revision)
        payload = {
            "catalog": {
                "device_id": self.get("device"),
                "snapshot_id": str(uuid.uuid4()),
                "expected_revision": revision,
                "items": items,
            }
            if self.config()["share_catalog"] or revision
            else None,
            "observations": {"device_id": self.get("device"), "items": observations},
        }
        path = self.directory / "sync.json"
        write_json(path, payload)
        data = path.read_bytes()
        return {
            "path": str(path),
            "request": {
                "request_id": str(uuid.uuid4()),
                "device_id": self.get("device"),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
            "shared_names": len(items),
            "observed_imports": len(observations),
        }

    def transfer(self, ticket_path):
        t = json.loads(Path(ticket_path).read_text(encoding="utf-8"))
        u = urllib.parse.urlsplit(t["url"])
        if (
            t.get("contract") != "zhixu-host-transfer-v1"
            or t.get("knowledge_base_id") != self.library
            or t.get("device_id") != self.get("device")
            or urllib.parse.urlunsplit((u.scheme, u.netloc, "", "", "")) != self.origin
            or u.query
            or u.fragment
        ):
            raise ValueError("传输许可与当前空间/设备不匹配")
        if not re.fullmatch("/v2/host-transfers/" + re.escape(self.library) + r"/[0-9a-f-]{36}", u.path):
            raise ValueError("传输地址不合法")
        token = t["token"]
        if not re.fullmatch(r"zxt_[A-Za-z0-9_-]{40,80}", token):
            raise ValueError("传输凭据无效")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError("不向重定向地址发送传输凭据")

        opener = urllib.request.build_opener(NoRedirect())

        def request(method, suffix="", body=None):
            req = urllib.request.Request(
                t["url"] + suffix,
                data=body,
                method=method,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/octet-stream"},
            )
            try:
                with opener.open(req, timeout=35) as r:
                    return json.loads(r.read(2 * 1024**2))
            except urllib.error.HTTPError as e:
                raise ValueError(f"传输返回 HTTP {e.code}，保留原件，重新通过同一 MCP 准备许可") from None

        if t["kind"] == "catalog":
            path = self.directory / "sync.json"
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != t["spec"]["sha256"]:
                raise ValueError("清单变化，重新导出并申请许可")
            result = request("PUT", body=data)
            if result.get("catalog"):
                self.put("catalog_revision", result["catalog"]["revision"])
        elif t["kind"] == "upload":
            spec = t["spec"]
            row = self.document(spec["document_id"], spec["file"]["sha256"])
            if spec["file"]["size"] != row["size"] or spec["file"]["filename"] != Path(row["path"]).name:
                raise ValueError("许可文件与原件不匹配")
            with original(self.scoped(row)) as (f, before):
                if digest(f) != row["sha"]:
                    raise ValueError("原件变化，停止上传")
                state = request("GET")
                offset = state["upload"]["offset"]
                f.seek(offset)
                size = min(t["chunk_bytes"], 8 * 1024**2)
                if not 0 < size <= 8 * 1024**2 or not 0 <= offset <= before.st_size:
                    raise ValueError("上传位置或分块大小不合法")
                while offset < before.st_size:
                    chunk = f.read(min(size, before.st_size - offset))
                    if not chunk:
                        raise ValueError("本机原件读取不完整")
                    r = request("PUT", "?offset=" + str(offset), chunk)
                    if r["offset"] != offset + len(chunk):
                        raise ValueError("服务端上传位置未按本次数据推进")
                    offset = r["offset"]
                f.seek(0)
                if digest(f) != row["sha"]:
                    raise ValueError("上传时原件变化，禁止完成收录")
            result = request("POST", "/complete", b"")
            self.db.execute(
                "INSERT OR REPLACE INTO imports VALUES(?,?,?)",
                (row["id"], result["source"]["id"], row["sha"]),
            )
        else:
            raise ValueError("未知传输类型")
        self.db.commit()
        Path(ticket_path).unlink(missing_ok=True)
        return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--library", required=True)
    p.add_argument("--origin", required=True)
    p.add_argument("--state")
    commands = p.add_subparsers(dest="cmd", required=True)
    c = commands.add_parser("configure")
    c.add_argument("--settings", required=True)
    c = commands.add_parser("scan")
    c.add_argument("--seconds", type=int, default=25)
    commands.add_parser("status")
    c = commands.add_parser("search")
    c.add_argument("query")
    c.add_argument("--titles-only", action="store_true")
    c = commands.add_parser("read")
    c.add_argument("--id", required=True)
    c.add_argument("--hash", required=True)
    c.add_argument("--offset", type=int, default=0)
    c = commands.add_parser("upload-request")
    c.add_argument("--id", required=True)
    c.add_argument("--hash", required=True)
    c.add_argument("--scope", choices=["self", "reference"], required=True)
    c = commands.add_parser("export-sync")
    c.add_argument("--revision", type=int)
    c = commands.add_parser("transfer")
    c.add_argument("--ticket", required=True)
    c = commands.add_parser("catalog")
    c.add_argument("--offset", type=int, default=0)
    c = commands.add_parser("changes")
    c.add_argument("--after", type=int, default=0)
    args = p.parse_args()
    index = Index(args.library, args.origin, args.state)
    try:
        if args.cmd == "configure":
            result = index.configure(json.loads(Path(args.settings).read_text(encoding="utf-8")))
        elif args.cmd == "scan":
            result = index.scan(max(1, min(60, args.seconds)))
        elif args.cmd == "status":
            result = index.status()
        elif args.cmd == "search":
            result = index.search(args.query, titles_only=args.titles_only)
        elif args.cmd == "read":
            result = index.read(args.id, args.hash, max(0, args.offset))
        elif args.cmd == "upload-request":
            result = index.upload_request(args.id, args.hash, args.scope)
        elif args.cmd == "export-sync":
            result = index.export_sync(args.revision)
        elif args.cmd == "transfer":
            result = index.transfer(args.ticket)
        elif args.cmd == "changes":
            result = {
                "items": [
                    dict(r)
                    for r in index.db.execute(
                        "SELECT *FROM changes WHERE seq>?ORDER BY seq LIMIT 100", (max(0, args.after),)
                    )
                ]
            }
        else:
            result = {
                "items": [
                    dict(r)
                    for r in index.db.execute(
                        "SELECT id,path,sha AS sha256,size,status,coverage FROM docs ORDER BY path LIMIT 100 OFFSET ?",
                        (max(0, args.offset),),
                    )
                ],
                "device_id": index.get("device"),
            }
        print(dump(result))
    finally:
        index.db.close()


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--parse-file":
            parse_worker(sys.argv[2])
        else:
            main()
    except Exception as e:
        # HTTP errors omit signed URLs, authorization headers and response bodies.
        print(
            dump({
                "error": str(e) if isinstance(e, ValueError) else type(e).__name__,
                "completed": False,
                **({"sqlite_error": e.sqlite_errorname}
                   if isinstance(e, sqlite3.Error) and hasattr(e, "sqlite_errorname") else {}),
            }),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
