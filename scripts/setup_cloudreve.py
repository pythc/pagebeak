"""Initialize only the dedicated, empty local Cloudreve sidecar; never print credentials."""

import base64
import json
import secrets

import httpx
from akb.config import ROOT


def main():
    target = ROOT / "secrets/cloudreve.json"
    if target.exists():
        print("Cloudreve credentials already configured; left unchanged")
        return
    client = httpx.Client(base_url="http://127.0.0.1:5878", timeout=60, trust_env=False)

    def call(method, path, **kwargs):
        r = client.request(method, "/api/v4/" + path, **kwargs)
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Cloudreve {path}: code={data.get('code')}")
        return data.get("data")

    # These accounts are for this isolated service, never end-user accounts.
    admin_password, password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    recovery = ROOT / "secrets/cloudreve-bootstrap.json"
    recovery.parent.mkdir(mode=0o700, exist_ok=True)
    if recovery.exists():
        stored = json.loads(recovery.read_text())
        admin_password, password = stored["admin_password"], stored["password"]
    else:
        recovery.touch(mode=0o600)
        recovery.write_text(json.dumps({"admin_password": admin_password, "password": password}))
        call(
            "POST",
            "user",
            json={"email": "operator@akb.local", "password": admin_password, "language": "zh-CN"},
        )
    login = call("POST", "session/token", json={"email": "operator@akb.local", "password": admin_password})
    client.headers["Authorization"] = "Bearer " + login["token"]["access_token"]
    group = call("GET", "admin/group/2")
    group["max_storage"] = 1024**4  # Aggregate sidecar ceiling; AKB enforces individual quotas.
    group["permissions"] = base64.b64encode(bytes([0x48, 0x10])).decode()  # DAV, proxy, explicit delete only.
    call("PUT", "admin/group/2", json={"group": group})
    # Disable public registration after provisioning. Sidecar has no public ingress.
    try:
        call("POST", "user", json={"email": "storage@akb.local", "password": password, "language": "zh-CN"})
    except RuntimeError:
        pass  # A previous attempt may have created the account; login must still succeed.
    call("PATCH", "admin/settings", json={"settings": {"register_enabled": "0", "siteName": "知序文件存储"}})
    service = call("POST", "session/token", json={"email": "storage@akb.local", "password": password})
    client.headers["Authorization"] = "Bearer " + service["token"]["access_token"]
    dav = call("PUT", "devices/dav", json={"uri": "cloudreve://my", "name": "知识库存储网关", "proxy": True})
    target.touch(mode=0o600)
    target.write_text(
        json.dumps(
            {
                "url": "http://127.0.0.1:5878",
                "email": "storage@akb.local",
                "password": password,
                "dav_password": dav["password"],
            }
        )
    )
    print("Cloudreve non-admin storage account and server-only WebDAV credential configured")


if __name__ == "__main__":
    main()
