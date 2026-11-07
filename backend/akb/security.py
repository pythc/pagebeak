import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException, Request
from sqlalchemy import select

from akb.models import Credential, User, now

SCOPES = {"knowledge:read", "knowledge:write", "profile:read", "forms:write"}


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    _, salt, expected = encoded.split("$")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return hmac.compare_digest(actual.hex(), expected)


def digest_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_credential(session, user_id: str, kind: str, label="browser", scopes=None):
    token = ("akb_" if kind == "api" else "ses_") + secrets.token_urlsafe(32)
    credential = Credential(
        digest=digest_token(token),
        user_id=user_id,
        kind=kind,
        label=label,
        scopes=sorted(scopes or SCOPES),
        csrf=secrets.token_urlsafe(24),
        expires_at=now() + timedelta(days=30 if kind == "api" else 7),
    )
    session.add(credential)
    session.flush()
    return token, credential


@dataclass
class Principal:
    user_id: str
    tenant_id: str
    display_name: str
    credential_id: str
    scopes: set[str]
    kind: str
    csrf: str
    is_admin: bool = False


def authenticate(request: Request) -> Principal:
    authorization = request.headers.get("authorization", "")
    bearer = authorization.startswith("Bearer ")
    token = authorization[7:] if bearer else request.cookies.get("akb_session", "")
    if not token or len(token) > 256:
        raise HTTPException(401, "请先登录或提供有效的个人访问令牌")
    with request.app.state.db.session() as session:
        credential = session.scalar(
            select(Credential).where(
                Credential.digest == digest_token(token),
                Credential.revoked.is_(False),
                Credential.expires_at > now(),
            )
        )
        if (
            not credential
            or (bearer and credential.kind != "api")
            or (not bearer and credential.kind != "browser")
        ):
            raise HTTPException(401, "登录或访问令牌已失效")
        user = session.get(User, credential.user_id)
        expected = request.headers.get("x-akb-knowledge-base")
        if expected and not hmac.compare_digest(expected, user.tenant_id):
            raise HTTPException(409, "连接身份不匹配：此令牌不属于指定知识库，操作已停止")
        if not bearer and request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(supplied, credential.csrf):
                raise HTTPException(403, "请求校验失败，请刷新后重试")
        return Principal(
            user.id,
            user.tenant_id,
            user.display_name,
            credential.id,
            set(credential.scopes),
            credential.kind,
            credential.csrf,
            user.is_admin,
        )


def require(principal: Principal, scope: str):
    if scope not in principal.scopes:
        raise HTTPException(403, "此访问令牌没有执行该操作的权限")


def browser_only(principal: Principal):
    if principal.kind != "browser":
        raise HTTPException(403, "此操作需要在网页中登录并由本人完成")
