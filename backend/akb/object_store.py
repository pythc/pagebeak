"""Private Cloudreve gateway. Browser and Agent never receive provider credentials or URLs."""

import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import HTTPException

PREFIX = "cloudreve:"


class Cloudreve:
    def __init__(self, settings):
        if not settings.cloudreve_credentials:
            raise OSError("Cloudreve is not configured")
        try:
            config = json.loads(settings.cloudreve_credentials.read_text())
            for key in ("url", "email", "password", "dav_password"):
                if not isinstance(config[key], str) or not config[key]:
                    raise ValueError("Invalid provider configuration")
        except (ValueError, KeyError) as exc:
            raise OSError("Invalid Cloudreve configuration") from exc
        self.config = config
        self.url = config["url"].rstrip("/")
        self.client = httpx.Client(
            base_url=self.url, timeout=120, trust_env=False, headers={"Accept-Encoding": "identity"}
        )

    def close(self):
        self.client.close()

    def path(self, key):
        key = key.removeprefix(PREFIX)
        if not re.fullmatch(r"[a-f0-9-]{36}/[a-f0-9-]{36}", key):
            raise OSError("Invalid private object key")
        return "/dav/akb/" + key

    def request(self, method, path, **kwargs):
        try:
            return self.client.request(
                method, path, auth=(self.config["email"], self.config["dav_password"]), **kwargs
            )
        except httpx.HTTPError as exc:
            raise OSError("Cloudreve storage unavailable") from exc

    def put(self, key, path):
        target = self.path(key)
        for folder in ("/dav/akb", target.rsplit("/", 1)[0]):
            response = self.request("MKCOL", folder)
            if response.status_code not in (201, 405):
                raise OSError("Cloudreve folder creation failed")
        # Immutable keys prevent versions from accumulating outside the product quota.
        head = self.request("HEAD", target)
        if head.status_code == 200:
            with self.materialize(key) as existing:
                if digest(existing) == digest(path):
                    return
            raise OSError("Immutable object conflict")
        if head.status_code != 404:
            raise OSError("Cloudreve object lookup failed")
        with path.open("rb") as stream:
            response = self.request(
                "PUT", target, content=stream, headers={"Content-Length": str(path.stat().st_size)}
            )
        if response.status_code not in (200, 201, 204):
            raise OSError("Cloudreve upload failed")
        head = self.request("HEAD", target)
        if head.status_code != 200 or int(head.headers.get("content-length", -1)) != path.stat().st_size:
            raise OSError("Cloudreve upload verification failed")

    @contextmanager
    def materialize(self, key, expected_sha=None, expected_size=None):
        with tempfile.TemporaryDirectory(prefix="akb-cloudreve-") as temp:
            path = Path(temp) / "original"
            if expected_size is not None and shutil.disk_usage(temp).free < expected_size + 256 * 1024**2:
                raise OSError("Insufficient parsing temporary space")
            hasher, size = hashlib.sha256(), 0
            try:
                with self.client.stream(
                    "GET", self.path(key), auth=(self.config["email"], self.config["dav_password"])
                ) as response:
                    response.raise_for_status()
                    with path.open("wb") as out:
                        for chunk in response.iter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > (expected_size if expected_size is not None else 2 * 1024**3):
                                raise OSError("Provider object exceeds expected size")
                            hasher.update(chunk)
                            out.write(chunk)
            except httpx.HTTPError as exc:
                raise OSError("Cloudreve download failed") from exc
            if expected_sha and hasher.hexdigest() != expected_sha:
                raise OSError("Provider object checksum mismatch")
            if expected_size is not None and size != expected_size:
                raise OSError("Provider object size mismatch")
            yield path

    def delete(self, key):
        path = self.path(key)
        head = self.request("HEAD", path)
        if head.status_code == 404:
            return
        if head.status_code != 200:
            raise OSError("Cloudreve unavailable; deletion not acknowledged")
        try:
            login = self.client.post(
                "/api/v4/session/token",
                json={"email": self.config["email"], "password": self.config["password"]},
            ).json()
            if login.get("code") != 0:
                raise OSError("Cloudreve login failed")
            token = login["data"]["token"]["access_token"]
            result = self.client.request(
                "DELETE",
                "/api/v4/file",
                headers={"Authorization": "Bearer " + token},
                json={
                    "uris": ["cloudreve://my/akb/" + key.removeprefix(PREFIX)],
                    "skip_soft_delete": True,
                    "unlink": False,
                },
            ).json()
            if result.get("code") != 0:
                raise OSError("Cloudreve deletion incomplete")
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise OSError("Cloudreve deletion failed") from exc
        if self.request("HEAD", path).status_code != 404:
            raise OSError("Cloudreve object still available")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def local_original(settings, key, expected_sha=None, expected_size=None, heartbeat=lambda **_: None):
    if key.startswith("native:"):
        from akb.native import local_original as native_original

        with native_original(settings, key, expected_sha, expected_size, heartbeat=heartbeat) as path:
            yield path
        return
    if not key.startswith(PREFIX):
        from akb.services import object_path

        yield object_path(settings, key)
        return
    provider = Cloudreve(settings)
    try:
        with provider.materialize(key, expected_sha, expected_size) as path:
            yield path
    finally:
        provider.close()


def delete_original(settings, key):
    if key.startswith("native:"):
        raise OSError("Native files must be managed through their version-aware native interface")
    if key.startswith(PREFIX):
        provider = Cloudreve(settings)
        try:
            provider.delete(key)
        finally:
            provider.close()
    else:
        from akb.services import object_path

        object_path(settings, key).unlink(missing_ok=True)


def publish(settings, tenant_id, source_id, path, session):
    key = PREFIX + f"{tenant_id}/{source_id}"
    journal = settings.object_root().parent / "cloudreve-journal" / (source_id + ".json")
    journal.parent.mkdir(mode=0o700, exist_ok=True)
    if not journal.exists():
        with tempfile.NamedTemporaryFile(mode="w", dir=journal.parent, delete=False) as stream:
            json.dump(
                {
                    "tenant_id": tenant_id,
                    "source_id": source_id,
                    "key": key,
                    "sha256": digest(path),
                    "created_at": datetime.now(UTC).isoformat(),
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
            temp_path = stream.name
        os.replace(temp_path, journal)
    provider = None
    try:
        provider = Cloudreve(settings)
        provider.put(key, path)
    except OSError as exc:
        raise HTTPException(503, "文件存储暂不可用，请稍后重试") from exc
    finally:
        if provider:
            provider.close()
    session.info.setdefault("published_journals", []).append(journal)
    return key
