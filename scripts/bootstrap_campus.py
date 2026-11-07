"""Operator-only provisioning for the dedicated campus database and file service.

Never run this against another application's services. Runtime does not inherit
the migration URL. Existing role credentials are verified, never rotated.
"""
import argparse
import json
import secrets

import httpx
import psycopg
from akb.config import Settings
from akb.weknora_client import private_write
from psycopg import sql
from sqlalchemy.engine import make_url


def database(settings):
    admin = make_url(settings.migration_database_url)
    if admin.database != "akb":
        raise RuntimeError("This bootstrap only supports the dedicated akb database")
    with psycopg.connect(admin.set(drivername="postgresql").render_as_string(hide_password=False), autocommit=True) as connection:
        for role, raw in (("akb_app", settings.database_url), ("akb_worker", settings.worker_database_url)):
            url = make_url(raw)
            if (url.username, url.host, url.port, url.database) != (role, admin.host, admin.port, admin.database):
                raise RuntimeError("Runtime database does not match the dedicated migration target")
            exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
            if exists:
                with psycopg.connect(url.set(drivername="postgresql").render_as_string(hide_password=False)) as runtime:
                    flags = runtime.execute("SELECT rolsuper,rolbypassrls,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user").fetchone()
                    if any(flags):
                        raise RuntimeError("Existing role has unexpected privileges; refusing silent changes")
            else:
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}").format(sql.Identifier(role), sql.Literal(url.password)))


def files(settings, allow_empty=False):
    target = settings.provisioner_file
    if not target or target.is_symlink():
        raise RuntimeError("Set a dedicated regular provisioner credential file")
    if not target.exists():
        if not allow_empty:
            raise RuntimeError("First provisioning requires --fresh-files after checking an empty dedicated volume")
        private_write(target, {"email": "operator@zhixu.invalid", "password": secrets.token_urlsafe(40)})
    identity = json.loads(target.read_text())
    with httpx.Client(base_url=settings.native_cloudreve_url + "/api/v4/", timeout=30, trust_env=False) as client:
        def call(method, path, **kwargs):
            result = client.request(method, path, **kwargs)
            result.raise_for_status()
            data = result.json()
            if data.get("code") != 0:
                raise RuntimeError(f"File service operation {path} failed, code={data.get('code')}")
            return data.get("data")

        credentials = {k: identity[k] for k in ("email", "password")}
        try:
            login = call("POST", "session/token", json=credentials)
        except RuntimeError:
            if not allow_empty:
                raise
            call("POST", "user", json={**credentials, "language": "zh-CN"})
            login = call("POST", "session/token", json=credentials)
        client.headers["Authorization"] = "Bearer " + login["token"]["access_token"]
        group = call("GET", "admin/group/2")  # Also verifies this is an operator, not a normal user.
        group["max_storage"] = settings.default_quota_bytes
        call("PUT", "admin/group/2", json={"group": group})
        policy = call("GET", "admin/policy/1")
        if policy.get("type") != "local":
            raise RuntimeError("Unexpected file storage provider; do not overwrite")
        policy["max_size"] = settings.upload_limit
        call("PUT", "admin/policy/1", json={"policy": policy})
        # This must match the externally reachable canonical origin; otherwise
        # native file sync correctly refuses the mismatched download links.
        call("PATCH", "admin/settings", json={"settings": {
            "siteName": "知序", "siteURL": settings.native_origin, "register_enabled": "0",
        }})
        checked_group, checked_policy = call("GET", "admin/group/2"), call("GET", "admin/policy/1")
        if checked_group["max_storage"] != settings.default_quota_bytes or checked_policy["max_size"] != settings.upload_limit:
            raise RuntimeError("File service limits did not persist")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("database", "files"))
    parser.add_argument("--fresh-files", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    try:
        if args.component == "database":
            database(settings)
        else:
            files(settings, args.fresh_files)
    except Exception as exc:
        # Operator exceptions can embed SQL parameters/HTTP credentials. Keep
        # the normal output secret-free; do not expose a full exception trace.
        raise SystemExit(f"Bootstrap failed ({type(exc).__name__}); configuration and existing data retained") from None
    print(f"Dedicated {args.component} provisioned and verified; credentials stay on the server")


if __name__ == "__main__":
    main()
