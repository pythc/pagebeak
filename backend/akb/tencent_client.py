"""Private, fixed-scope Tencent APIs. Never forward caller-selected isolation IDs."""
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException


class TencentClient:
    def __init__(self, settings, tenant_id):
        self.settings, self.tenant = settings, tenant_id

    def call(self, component, path, body=None):
        base = self.settings.memory_core_url if component == "core" else self.settings.memory_wiki_url
        url = urlsplit(base)
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or url.username or url.password or url.path or url.query or url.fragment):
            raise HTTPException(503, "腾讯 Memory 必须通过本机私有服务访问")
        try:
            key = self.settings.memory_service_key_file.read_text().strip()
            if len(key) < 32:
                raise ValueError("missing service key")
            payload = {**(body or {}), "team_id": self.tenant, "user_id": self.tenant,
                       "agent_id": "zhixu-workbuddy"}
            with httpx.Client(base_url=base, timeout=15, trust_env=False, follow_redirects=False) as client:
                response = client.post(path, json=payload, headers={
                    "Authorization": "Bearer " + key, "x-tdai-service-id": self.tenant})
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 0:
                raise ValueError("upstream rejected operation")
            return data.get("data") or {}
        except (OSError, ValueError, httpx.HTTPError) as exc:
            # Do not forward upstream request dumps, document content, or service credentials.
            raise HTTPException(503, "腾讯 Memory 暂不可用；已停止操作，没有回退到旧知识库") from exc
