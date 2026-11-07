"""WeKnora native HTTP storage, behind the existing publication-fence protocol.

Two scoped KBs per teacher: source exports and agent-authored context. Only the
AKB evidence store is authoritative for filling. No Tencent process is involved.
"""
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException

STRATEGY = {"vector_enabled": False, "keyword_enabled": True, "wiki_enabled": False, "graph_enabled": False}
KB_CONFIG = {"type": "document", "indexing_strategy": STRATEGY, "embedding_model_id": "", "summary_model_id": "",
             "question_generation_config": {"enabled": False}, "auto_tag_config": {"enabled": False},
             "profile_config": {"enabled": False}, "extract_config": {"enabled": False},
             "image_processing_config": {"enable_multimodal": False}, "chunking_config": {"chunk_size": 1024, "chunk_overlap": 80}}


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + "." + uuid4().hex)
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


class WeKnoraClient:
    def __init__(self, settings, tenant_id):
        self.settings, self.tenant = settings, str(UUID(tenant_id))
        u = urlsplit(settings.weknora_url)
        if u.scheme != "http" or u.hostname not in {"127.0.0.1", "localhost", "::1"} or u.username or u.password or u.path or u.query or u.fragment:
            raise HTTPException(503, "WeKnora 必须是本机私有后端")
        self.path = Path(settings.weknora_secrets_dir) / (self.tenant + ".json")
        self.credentials = None

    def request(self, method, path, body=None, headers=None, params=None):
        with httpx.Client(base_url=self.settings.weknora_url, timeout=20, trust_env=False, follow_redirects=False) as client:
            r = client.request(method, path, json=body, headers=headers or {}, params=params)
        r.raise_for_status()
        data = r.json()
        if data.get("success") is False:
            raise ValueError("WeKnora rejected request")
        return data

    def mode(self):
        data = self.request("GET", "/zhixu/mode")
        if data.get("contract") != "zhixu-weknora-v1" or data.get("external_executor") is not True or data.get("backend_inference") is not False:
            raise ValueError("external-only runtime required")
        return data

    def connect(self):
        if self.credentials:
            return self.credentials
        self.mode()
        if self.path.is_symlink():
            raise ValueError("invalid credential file")
        c = json.loads(self.path.read_text()) if self.path.exists() else None
        if c is None:
            # Persist before registration: a timeout can then recover with the same identity.
            c = {"email": f"zhixu-{self.tenant}@example.invalid", "password": "Aa1_" + secrets.token_urlsafe(18),
                 "origin": self.settings.weknora_url}
            private_write(self.path, c)
        if c.get("origin") != self.settings.weknora_url:
            raise ValueError("backend identity changed; explicit migration required")
        if not c.get("key"):
            try:
                login = self.request("POST", "/api/v1/auth/login", {k: c[k] for k in ("email", "password")})
            except (httpx.HTTPStatusError, ValueError):
                self.request("POST", "/api/v1/auth/register", {k: c[k] for k in ("email", "password")} | {"username": "zhixu-" + self.tenant[:12]})
                login = self.request("POST", "/api/v1/auth/login", {k: c[k] for k in ("email", "password")})
            headers = {"Authorization": "Bearer " + login["token"]}
            tid = login["active_tenant"]["id"]
            existing = self.request("GET", "/api/v1/knowledge-bases", headers=headers)["data"]
            for label in ("sources", "context"):
                name = "zhixu-" + label
                hits = [kb for kb in existing if kb["name"] == name]
                if len(hits) > 1:
                    raise ValueError("duplicate KB binding")
                kb = hits[0] if hits else self.request("POST", "/api/v1/knowledge-bases", KB_CONFIG | {"name": name}, headers)["data"]
                c[label] = kb["id"]
            issued = self.request("POST", f"/api/v1/tenants/{tid}/api-keys", {
                "name": "zhixu-private", "full_access": False,
                "knowledge_base_ids": [c["sources"], c["context"]], "capabilities": ["retrieve", "ingest"]}, headers)["data"]
            c["key"] = issued["token"]
            private_write(self.path, c)
        self.credentials = c
        # Every new client checks effective config; missing/changed model settings fail closed.
        for label in ("sources", "context"):
            kb = self.api("GET", "/knowledge-bases/" + c[label])["data"]
            if kb.get("indexing_strategy") != STRATEGY or kb.get("summary_model_id") or kb.get("embedding_model_id"):
                raise ValueError("backend pipeline drift")
            for name in ("question_generation_config", "auto_tag_config", "profile_config", "extract_config"):
                if (kb.get(name) or {}).get("enabled"):
                    raise ValueError("backend generation enabled")
        return c

    def api(self, method, path, body=None, params=None):
        c = self.credentials or self.connect()
        return self.request(method, "/api/v1" + path, body, {"X-API-Key": c["key"]}, params)

    def documents(self, label, keyword=None):
        kb = self.connect()[label]
        result = []
        for page in range(1, 10001):
            data = self.api("GET", f"/knowledge-bases/{kb}/knowledge", params={"page": page, "page_size": 100, "zhixu_compact": "1", **({"keyword": keyword} if keyword else {})})
            items = data["data"] or []
            result.extend(items)
            if len(items) < 100:
                return result
        raise ValueError("inventory exceeds supported page bound")

    @staticmethod
    def decode(doc):
        meta = doc.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        return json.loads(meta["content"])["body"]

    def get(self, doc):
        return self.api("GET", "/knowledge/" + str(UUID(doc["id"])))["data"]

    def write_document(self, label, ref, content):
        # JSON-escaped text is inert and retains exact Unicode through Markdown sanitization.
        body = json.dumps({"body": content}, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        if len(body) > 200000:
            raise ValueError("export chunk too large")
        hits = [d for d in self.documents(label, keyword=ref) if d["title"] == ref]
        if len(hits) > 1:
            raise ValueError("ambiguous remote revision")
        doc = hits[0] if hits else self.api("POST", f"/knowledge-bases/{self.connect()[label]}/knowledge/manual", {
            "title": ref, "content": body, "status": "publish", "channel": "api",
            "process_config": {"summary_enabled": False, "enable_multimodel": False, "graph_enabled": False, "image_attrs_enabled": False, "image_vector_enabled": False, "question_generation_config": {"enabled": False}}})["data"]
        if self.decode(self.get(doc)) != content:
            raise ValueError("immutable revision mismatch")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.get(doc)
            if state["parse_status"] == "completed":
                return doc["id"]
            if state["parse_status"] in {"failed", "cancelled"}:
                raise ValueError("native keyword indexing failed")
            time.sleep(0.15)
        raise ValueError("native indexing pending; retry same request")

    def remove(self, label, refs):
        for d in self.documents(label):
            if d["title"] in refs:
                self.api("DELETE", "/knowledge/" + str(UUID(d["id"])))

    def call(self, component, path, body=None):
        try:
            return self._call(component, path, body or {})
        except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
            raise HTTPException(503, "WeKnora 暂不可用或索引尚未完成；保留任务并重试，没有切回旧引擎") from exc

    def _call(self, component, path, b):
        c = self.connect()
        if "wiki_id" in b and b["wiki_id"] != c["sources"]:
            raise ValueError("invalid tenant KB binding")
        if path == "/v3/wiki/create":
            return {"wiki_id": c["sources"]}
        if path == "/v3/wiki/raw/write":
            for f in b["files"]:
                self.write_document("sources", f["filename"], f["content"])
            return {}
        if path == "/v3/zhixu/publish":
            for p in b["pages"]:
                self.write_document("context", p["ref"], p["content"])
            return {}
        if path == "/v3/conversation/add":
            ref = "note-" + b["session_id"] + ".md"
            self.write_document("context", ref, json.dumps(b["messages"], ensure_ascii=False))
            return {"accepted_ids": [ref + ":" + str(i) for i in range(len(b["messages"]))]}
        if path in {"/v3/wiki/raw/rm", "/v3/zhixu/forget", "/v3/conversation/delete"}:
            refs = b.get("filenames", b.get("refs", []))
            if "session_ids" in b:
                refs = ["note-" + s + ".md" for s in b["session_ids"]]
            self.remove("sources" if "raw/rm" in path else "context", refs)
            return {}
        if path == "/v3/wiki/page/read":
            by_ref = {d["title"]: d for d in self.documents("context")}
            return {"items": [{"ref": r, "content": self.decode(self.get(by_ref[r]))} if r in by_ref else {"ref": r, "not_found": True} for r in b["refs"]]}
        if path == "/v3/conversation/query":
            ref = "note-" + b["session_id"] + ".md"
            docs = [d for d in self.documents("context") if d["title"] == ref]
            messages = json.loads(self.decode(self.get(docs[0]))) if docs else []
            return {"messages": [m | {"id": ref + ":" + str(i)} for i, m in enumerate(messages)]}
        if path == "/v3/zhixu/inventory":
            labels = ("sources", "context") if component == "wiki" else ("context",)
            result = {"bytes": 0, "pages": [], "raw": [], "sessions": []}
            for label in labels:
                for d in self.documents(label):
                    note = d["title"].startswith("note-")
                    if (component == "core") != note:
                        continue
                    result["bytes"] += d["payload_bytes"]
                    if note:
                        result["sessions"].append(d["title"][5:].removesuffix(".md"))
                    else:
                        result["raw" if label == "sources" else "pages"].append({"ref": d["title"]})
            return result
        if path in {"/v3/wiki/search", "/v3/conversation/search"}:
            hits = self.api("POST", f"/knowledge-bases/{c['context']}/hybrid-search", {
                "query_text": b["query"], "match_count": b.get("limit", 30), "disable_vector_match": True})["data"] or []
            docs = {d["id"]: d for d in self.documents("context")}
            pages, notes = [], []
            for h in hits:
                d = docs.get(h.get("knowledge_id"))
                if not d:
                    continue
                if d["title"].startswith("note-"):
                    for i, m in enumerate(json.loads(self.decode(self.get(d)))):
                        notes.append({"id": d["title"] + ":" + str(i), "content": m["content"], "score": h.get("score", 0)})
                else:
                    pages.append({"path": d["title"], "snippet": h.get("content", ""), "score": h.get("score", 0)})
            return {"results": pages, "messages": notes}
        if path == "/v3/zhixu/literal":
            # Native FTS indexes Chinese bigrams; avoid downloading every page for each query.
            return {"items": []}
        raise ValueError("unsupported storage operation")

    def search_sources(self, query, limit=30):
        try:
            c = self.connect()
            hits = self.api("POST", f"/knowledge-bases/{c['sources']}/hybrid-search", {
                "query_text": query, "match_count": limit, "disable_vector_match": True})["data"] or []
            docs = {d["id"]: d for d in self.documents("sources")}
            return [{"ref": docs[h["knowledge_id"]]["title"], "snippet": h.get("content", ""),
                     "score": h.get("score", 0)} for h in hits if h.get("knowledge_id") in docs]
        except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
            raise HTTPException(503, "WeKnora 原文检索不可用") from exc
