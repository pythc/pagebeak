"""Create an isolated native-frontend review identity, never expose the storage broker account."""

import base64
import json
import secrets

import httpx
from akb.config import ROOT


def main():
    target = ROOT / "secrets/cloudreve-review.json"
    if target.exists():
        print("Native frontend review account already exists; preserved")
        return
    bootstrap = json.loads((ROOT / "secrets/cloudreve-bootstrap.json").read_text())
    with httpx.Client(base_url="http://127.0.0.1:5878/api/v4/", timeout=30, trust_env=False) as client:

        def call(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 0:
                raise RuntimeError(f"Cloudreve {path}: code={result.get('code')}")
            return result["data"]

        admin = call(
            "POST",
            "session/token",
            json={"email": "operator@akb.local", "password": bootstrap["admin_password"]},
        )
        client.headers["Authorization"] = "Bearer " + admin["token"]["access_token"]
        group = call("GET", "admin/group/2")
        group.update(id=0, name="原版界面评审", max_storage=10 * 1024**3)
        # Upstream default ordinary-user permissions: share, shared downloads, redirected source.
        group["permissions"] = base64.b64encode(bytes([0x84, 0x08])).decode()
        group = call("PUT", "admin/group", json={"group": group})
        identity = {
            "email": "native-review@akb.local",
            "password": secrets.token_urlsafe(32),
            "url": "http://localhost:5878/",
        }
        user = call(
            "PUT",
            "admin/user",
            json={
                "user": {
                    "email": identity["email"],
                    "nick": "原版界面评审",
                    "status": "active",
                    "group_users": group["id"],
                },
                "password": identity["password"],
            },
        )
        identity["user_id"] = user["id"]
        target.touch(mode=0o600)
        target.write_text(json.dumps(identity))
        print("Isolated native frontend review account created; credentials remain server-local")


if __name__ == "__main__":
    main()
