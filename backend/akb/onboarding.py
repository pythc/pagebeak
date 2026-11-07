
"""First-use setup; interview answers are context, never confirmed profile facts."""
import json
import os
import secrets
import shlex
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Response
from pydantic import Field
from sqlalchemy import func, select

from akb import services
from akb.config import ROOT
from akb.models import AssistantGrant, ConnectionTicket, Credential, Onboarding, Source, now
from akb.schemas import Input
from akb.security import Principal, authenticate, browser_only, digest_token, issue_credential, require
from akb.workbuddy_package import engine_skill_root

SKILL_VERSION = "2026.10.17.1"
WEKNORA_SKILL_VERSION = "2026.10.31.1"


def current_skill_version(settings=None):
    return WEKNORA_SKILL_VERSION if settings and settings.knowledge_engine == "weknora" else SKILL_VERSION

QUESTIONS = {
    "preferred_name": "我该怎么称呼你？不方便说姓名的话，用昵称也可以。",
    "identity": "你目前主要做什么工作或学习？例如教师、企业职员、学生，也可以用自己的话说。",
    "goal": "你最希望我先帮你做哪件事？比如找资料、写材料，或者填写个人信息。",
}
AnswerKey = Literal["preferred_name", "identity", "goal", "local_documents"]


class ProgressInput(Input):
    expected_revision: int = Field(ge=1)
    answers: dict[AnswerKey, str] = Field(default_factory=dict, max_length=4)
    skipped: list[AnswerKey] | None = Field(default=None, max_length=3)
    complete: bool = False
    dismissed: bool | None = None


class StartInput(Input):
    skill_version: str


class InstallationInput(StartInput):
    channel: Literal["manual", "expert"] = "manual"


class RedeemInput(Input):
    code: str = Field(min_length=30, max_length=100)
    knowledge_base_id: UUID


def connection_config(settings, tenant_id, token):
    return {"type": "stdio", "command": sys.executable, "args": ["-m", "akb.mcp_server"],
            "cwd": str(ROOT), "env": {"AKB_API_URL": f"http://127.0.0.1:{settings.api_port}",
            "AKB_TOKEN": token, "AKB_EXPECTED_KB_ID": tenant_id}}


def state_dict(session, p, state, settings=None):
    from akb.workbuddy_package import PACKAGE_VERSION, initialization_link, package_identity

    credential = session.get(Credential, state.credential_id) if state.credential_id else None
    installed = active_installation(session, state)
    connected = bool(credential and not credential.revoked and credential.expires_at > now())
    if installed and installed["credential_id"] != state.credential_id:
        connected = False
    unanswered = [k for k in QUESTIONS if k not in state.answers and k not in state.skipped]
    prompts = QUESTIONS
    if settings and settings.knowledge_engine == 'weknora':
        prompts = {'local_documents': '为了更了解你的资料，是否允许我检索这台电脑？请选择：检索全部文件、检索指定文件夹、暂不检索。'}
        unanswered = [] if state.answers.get('local_documents') in {'all', 'folders', 'none'} else ['local_documents']
    count = session.scalar(select(func.count()).select_from(Source).where(
        Source.revoked_at.is_(None), Source.trashed_at.is_(None)))
    distribution = {"mode": "local_preview", "package_version": PACKAGE_VERSION,
                    "expert_name": package_identity(p.tenant_id),
                    "initialization_url": initialization_link(p.tenant_id, current_skill_version(settings)),
                    "one_click_ready": False, "reason": "尚未发布 WorkBuddy 市场资产"}
    if settings and settings.assistant_public_origin:
        from akb.assistant_package import NAME, package_version
        from akb.assistant_package import initialization_link as remote_initialization_link

        available = bool(settings.assistant_author_email)
        distribution = {"mode": "remote" if available else "remote_preview", "package_version": package_version(settings.knowledge_engine), "expert_name": NAME,
                        "initialization_url": remote_initialization_link(p.tenant_id, settings.assistant_public_origin),
                        "installation_available": available, "one_click_ready": False,
                        "reason": "学校共享助理包，通过 WorkBuddy 正常导入和授权" if available else "管理员尚未配置共享助理安装入口"}
    return {"revision": state.revision, "knowledge_base_id": p.tenant_id,
            "skill_version": current_skill_version(settings), "connected": connected,
            "installation": {k: v for k, v in installed.items() if k != "credential_id"} if installed else None,
            "started_at": state.started_at, "completed_at": state.completed_at,
            "dismissed": state.dismissed, "answers": state.answers, "skipped": state.skipped,
            "pending_questions": [{"key": key, "prompt": prompts[key]} for key in unanswered],
            "next_question": {"key": unanswered[0], "prompt": prompts[unanswered[0]]} if unanswered else None,
            "local_document_options": [{'value': k, 'label': v} for k, v in [('all', '检索全部文件'), ('folders', '检索指定文件夹'), ('none', '暂不检索')]],
            "source_count": count, "answers_usage": "仅用于初始化引导，未经复核不进入正式个人档案或填表",
            "distribution": distribution}


def create_ticket(session, p):
    # Replacing a not-yet-used package must not revoke a working connection.
    for ticket in session.scalars(select(ConnectionTicket).where(ConnectionTicket.used_at.is_(None))):
        ticket.used_at = now()
    code = "join_" + secrets.token_urlsafe(32)
    expires = now() + timedelta(minutes=15)
    session.add(ConnectionTicket(tenant_id=p.tenant_id, digest=digest_token(code),
                                issued_by=p.credential_id, expires_at=expires))
    return code, expires


def active_installation(session, state):
    installation = state.installation or {}
    credential_id = installation.get("credential_id")
    credential = session.get(Credential, credential_id) if credential_id else None
    if credential and not credential.revoked and credential.expires_at > now():
        return installation
    return None


def routes(app, db, settings):
    active_version = current_skill_version(settings)
    from akb import native, native_start

    def drive_identity(x_cloudreve_token: str = Header(min_length=20, max_length=10000),
                       x_akb_file_user: str | None = Header(default=None, max_length=80)):
        identity = native.verify_identity(settings, x_cloudreve_token)
        if x_akb_file_user is not None and identity["id"] != x_akb_file_user:
            raise HTTPException(409, "文件账号已变化，请刷新开始页；未执行接入操作")
        return identity

    @app.post("/v1/onboarding/installation")
    def installed(body: InstallationInput, p: Principal = Depends(authenticate)):
        require(p, "knowledge:write")
        require(p, "profile:read")
        if p.kind != "api":
            raise HTTPException(403, "安装收据需要安装器使用本连接回报")
        if body.skill_version != active_version:
            raise HTTPException(409, "安装技能版本不匹配，请返回开始页面更新")
        with db.session(p.tenant_id) as session:
            services.lock_tenant(session, p.tenant_id)
            state = session.scalar(select(Onboarding))
            prior = state.installation or {}
            if (prior.get("credential_id") != p.credential_id or prior.get("skill_version") != body.skill_version
                    or prior.get("channel", "manual") != body.channel):
                suffix = "-expert" if body.channel == "expert" else ""
                state.installation = {"credential_id": p.credential_id, "server_name": "zhixu-" + p.tenant_id + suffix,
                                      "skill_version": body.skill_version, "reported_at": now().isoformat(),
                                      "channel": body.channel}
                state.revision += 1
            return state_dict(session, p, state, settings)

    @app.get("/v1/onboarding")
    def read(p: Principal = Depends(authenticate)):
        require(p, "knowledge:read")
        require(p, "profile:read")
        with db.session(p.tenant_id) as session:
            return state_dict(session, p, session.scalar(select(Onboarding)), settings)

    @app.post("/v1/onboarding/start")
    def start(body: StartInput, p: Principal = Depends(authenticate)):
        require(p, "knowledge:write")
        require(p, "profile:read")
        if p.kind != "api":
            raise HTTPException(403, "需要助手通过知识库连接实际核验")
        if body.skill_version != active_version:
            raise HTTPException(409, "请重新安装开始页面提供的当前 Skill，再继续初始化")
        with db.session(p.tenant_id) as session:
            services.lock_tenant(session, p.tenant_id)
            state = session.scalar(select(Onboarding))
            installed = active_installation(session, state)
            shared = settings.assistant_public_origin and session.scalar(select(AssistantGrant.id).where(
                AssistantGrant.kind == "refresh", AssistantGrant.credential_id == p.credential_id,
                AssistantGrant.user_id == p.user_id, AssistantGrant.used_at.is_(None),
                AssistantGrant.expires_at > now()))
            if shared and (not installed or installed["credential_id"] != p.credential_id):
                from akb.assistant_package import NAME

                # Only record actual authenticated MCP initialization, never an
                # unobserved installation or consent alone. Preserve all answers.
                state.installation = {"credential_id": p.credential_id, "server_name": NAME,
                    "skill_version": body.skill_version, "reported_at": now().isoformat(),
                    "channel": "expert", "distribution": "shared_oauth"}
                installed = state.installation
            if installed and installed["credential_id"] != p.credential_id:
                raise HTTPException(409, "请选用开始页面显示的新安装连接，不能用旧连接替代核验")
            if state.credential_id != p.credential_id or not state.started_at:
                state.credential_id, state.started_at = p.credential_id, now()
                state.revision += 1
            return state_dict(session, p, state, settings)

    @app.patch("/v1/onboarding")
    def update(body: ProgressInput, p: Principal = Depends(authenticate)):
        require(p, "knowledge:write")
        require(p, "profile:read")
        if body.dismissed is not None:
            browser_only(p)
        with db.session(p.tenant_id) as session:
            services.lock_tenant(session, p.tenant_id)
            state = session.scalar(select(Onboarding))
            if state.revision != body.expected_revision:
                raise HTTPException(409, "初始化进度已更新，请重新读取后继续；已有回答未被覆盖")
            answers = dict(state.answers)
            for key, value in body.answers.items():
                if key == 'local_documents' and value not in {'all', 'folders', 'none'}:
                    raise HTTPException(422, '本机检索选项必须为 all、folders 或 none')
                if not value.strip() or len(value) > 1000:
                    raise HTTPException(422, "回答需为 1 至 1000 字；不愿提供可选择跳过")
                answers[key] = value.strip()
            skipped = list(dict.fromkeys(body.skipped if body.skipped is not None else state.skipped))
            skipped = [k for k in skipped if k not in body.answers]
            if body.skipped is not None:
                for key in skipped:
                    answers.pop(key, None)
            if body.complete:
                if not state_dict(session, p, state, settings)["connected"]:
                    raise HTTPException(409, "助手还未核验连接，不能标记完成")
                required = ['local_documents'] if settings.knowledge_engine == 'weknora' else QUESTIONS
                if any(k not in answers and (k not in skipped or k == 'local_documents') for k in required):
                    raise HTTPException(422, "请先回答或明确跳过当前问题")
            if body.answers or body.skipped is not None:
                state.completed_at = None
            state.answers, state.skipped = answers, skipped
            if body.complete:
                state.completed_at = now()
            if body.dismissed is not None:
                state.dismissed = body.dismissed
            state.revision += 1
            return state_dict(session, p, state, settings)

    @app.post("/v1/onboarding/package")
    def download_package(p: Principal = Depends(authenticate)):
        from akb.workbuddy_package import build_package

        browser_only(p)
        if settings.assistant_public_origin:
            from akb.assistant_package import published_package
            try:
                content = published_package(settings)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            return Response(content, media_type="application/zip", headers={
                "Content-Disposition": 'attachment; filename="zhixu-teacher-assistant.zip"',
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        with db.session(p.tenant_id) as session:
            services.lock_tenant(session, p.tenant_id)
            code, expires = create_ticket(session, p)
            content, name = build_package(settings, p.tenant_id, code, expires, active_version)
            return Response(content, media_type="application/zip", headers={
                "Content-Disposition": f'attachment; filename="{name}.zip"',
                "Cache-Control": "no-store, private", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"})

    @app.post("/v1/onboarding/package/launch", status_code=201)
    def launch_package(p: Principal = Depends(authenticate)):
        from uuid import uuid4

        from akb.workbuddy_entry import private_directory
        from akb.workbuddy_package import build_package

        browser_only(p)
        if settings.assistant_public_origin:
            from akb.assistant_package import installation
            try:
                return installation(settings, p.tenant_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        root = settings.workbuddy_package_dir
        private_directory(root)
        private_directory(root / p.tenant_id)
        for old in (root / p.tenant_id).glob("*.zip"):
            try:
                if (len(old.stem) == 32 and all(c in "0123456789abcdef" for c in old.stem)
                        and not old.is_symlink() and old.is_file() and old.stat().st_mtime < time.time() - 900):
                    old.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
        archive_path = root / p.tenant_id / (uuid4().hex + ".zip")
        try:
            with db.session(p.tenant_id) as session:
                services.lock_tenant(session, p.tenant_id)
                code, expires = create_ticket(session, p)
                content, name = build_package(settings, p.tenant_id, code, expires, active_version)
                fd = os.open(archive_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                command = shlex.join([sys.executable, str(ROOT / "scripts/install_workbuddy_package.py"),
                                      str(archive_path), "--workbuddy-dir", str(Path.home() / ".workbuddy")])
                prompt = ("请帮我安装这台电脑上的知序知识库管家接入包，并在同一对话直接完成初始化和开始整理资料。只执行下面的本机项目安装器，"
                          "保留正常授权，不改安全设置或信任记录，不展示包内接入码。\n"
                          f"{command}\n"
                          f"只接入知识库 {p.tenant_id}，专家和连接名 {name}。"
                          "安装器会将专家、随包技能和 MCP 依赖注册到我的专家；不会直接改 mcp.json。"
                          f"注册后立即读取可信技能 {engine_skill_root(settings.knowledge_engine) / 'SKILL.md'} 及 references/onboarding.md，"
                          "在当前任务中选用或按需连接上述 MCP，不要只报告安装成功就结束，也不要让我回网页点击初始化、另开对话或再次召唤专家。"
                          "若宿主显示连接授权，请我完成该必要授权后继续当前任务，不猜测授权或连接状态。"
                          f"工具可用后调用 get_knowledge_context 核对库 ID，再调用 start_onboarding(skill_version=\"{active_version}\") 和 read_onboarding。"
                          "结合当前可信对话了解称呼、身份/工作学习和优先用途，相关缺项可合并询问；已有信息不重复问。"
                          "用户一次提供的多项回答一并保存，具备信息时直接标记基础引导完成并继续整理现有资料、形成共用目录。"
                          "不替用户回答，不把登录名当真实身份，不扫描全盘、删除资料或向外提交。"
                          "连接确实不可用时说明具体阻塞及必要操作，不能用其他库或直接 HTTP 假装 MCP 已就绪。")
            return Response(json.dumps({
                "launch_url": "workbuddy://task?" + urlencode({"action": "start", "prompt": prompt,
                                                               "cwd": str(ROOT), "permissionMode": "default"}),
                "expires_at": expires.isoformat(), "expert_name": name,
                "status": "waiting_for_workbuddy", "knowledge_base_id": p.tenant_id,
            }, ensure_ascii=False), status_code=201, media_type="application/json",
                headers={"Cache-Control": "no-store, private"})
        except BaseException:
            archive_path.unlink(missing_ok=True)
            raise

    @app.post("/v1/onboarding/connection", status_code=201)
    def create_connection(p: Principal = Depends(authenticate)):
        browser_only(p)
        if settings.assistant_public_origin:
            raise HTTPException(409, "请通过共享助理的 OAuth 连接本人资料空间")
        with db.session(p.tenant_id) as session:
            services.lock_tenant(session, p.tenant_id)
            code, expires = create_ticket(session, p)
            command = shlex.join([sys.executable, str(ROOT / "scripts/install_workbuddy.py"),
                                  "--base-url", f"http://127.0.0.1:{settings.api_port}",
                                  "--knowledge-base-id", p.tenant_id, "--code", code])
            check_command = shlex.join([sys.executable, str(ROOT / "scripts/install_workbuddy.py"),
                                        "--base-url", f"http://127.0.0.1:{settings.api_port}",
                                        "--knowledge-base-id", p.tenant_id, "--check"])
            skill = str(engine_skill_root(settings.knowledge_engine) / "SKILL.md")
            authorization = (
                "我授权安装本库的 MCP 连接与知识库管家 Skill，读取和整理本账号资料、保存初始化回答，"
                "由当前 WorkBuddy 模型生成并保存知识页及明确偏好；不冒充用户本人确认，不覆盖人工修改。"
                "不授权扫描全盘、删除原文件或向外提交。\n"
                if settings.knowledge_engine in {"tencent", "weknora"} else
                "我授权安装本库的 MCP 连接与知识库管家 Skill，读取和整理本账号资料、保存初始化回答并提出候选；"
                "不授权扫描全盘、删除资料、确认个人事实或向外提交。\n"
            )
            prompt = ("请帮我连接我的知序个人知识库，并立即按照 personal-knowledge 技能开始初始化。我不熟悉 AI，请用日常语言引导。\n"
                      f"目标知识库：{p.tenant_id}。仅连接此库，不使用其他账号或连接代替。\n"
                      f"{authorization}"
                      "若安装遇到权限或运行环境错误，请停止并简短说明受阻步骤，通过宿主正常授权处理。"
                      "不要关闭沙箱或拦截层、修改注入/安全环境变量、跳过 Python 初始化、改动安装器源码或替我修改信任记录。\n"
                      f"1. 在这台电脑执行下面的安装命令。一次性接入码 15 分钟有效，不要展示或记录命令中的码和返回的凭据：\n{command}\n"
                      f"2. 读取本机可信技能源 {skill} 及 references/onboarding.md。安装器会备份并合并配置，保留其他服务和技能。\n"
                      f"3. 安装后在当前任务中选用或按需连接 zhixu-{p.tenant_id}；宿主要求连接授权时，完成后继续当前任务。"
                      "不要求回网页点击初始化或再次发送开始指令，也不预设重启、另开对话是必经步骤。"
                      "只有实际报错或工具确实不可见时，才说明具体阻塞与必要的恢复操作；不猜测按钮位置或信任状态。"
                      "不能用其他库的工具或直接 HTTP 代替 MCP。"
                      f"已有安装但页面未更新时，只核对，不重复安装或兑换接入码：\n{check_command}\n"
                      f"4. 工具可用后，先调用 get_knowledge_context 核对上述库 ID，再调用 start_onboarding，skill_version={active_version}，"
                      "接着 read_onboarding，在同一对话按照 Skill 开始了解需求。相关信息可以合并询问、多项回答一并保存并完成初始化；只记录我实际回答或明确跳过的内容，"
                      "不从登录昵称猜姓名身份，不重复问已有回答，不把初始化回答直接视为已确认的填表事实。\n"
                      "若你是不能操作本机的网页聊天助手，请如实说明，并让我把同一段文字发给这台电脑上的 WorkBuddy。")
            return {"prompt": prompt, "expires_at": expires, "knowledge_base_id": p.tenant_id}

    @app.post("/v1/onboarding/redeem")
    def redeem(body: RedeemInput):
        tenant_id = str(body.knowledge_base_id)
        with db.session(tenant_id) as session:
            ticket = session.scalar(select(ConnectionTicket).where(
                ConnectionTicket.digest == digest_token(body.code)).with_for_update())
            if not ticket or ticket.used_at or ticket.expires_at <= now():
                raise HTTPException(400, "接入指令已使用或过期，请回开始页面重新生成")
            issuer = session.get(Credential, ticket.issued_by)
            if not issuer or issuer.revoked or issuer.expires_at <= now():
                raise HTTPException(400, "原登录已失效，请重新登录并生成接入指令")
            token, credential = issue_credential(session, issuer.user_id, "api", "知序初次接入")
            ticket.used_at = now()
            return {"server_name": "zhixu-" + tenant_id,
                    "config": connection_config(settings, tenant_id, token),
                    "credential_id": credential.id, "skill_version": active_version}

    def native_state(identity):
        mode = ("remote" if settings.assistant_author_email else "remote_preview") if settings.assistant_public_origin else "local"
        if not native_start.linked_tenant(db, identity):
            return {"linked": False, "file_account_id": identity["id"], "assistant_mode": mode}
        p = native_start.principal(db, identity)
        return {"linked": True, "file_account_id": identity["id"], "assistant_mode": mode, "onboarding": read(p)}

    @app.get("/v1/native/start")
    def read_native_start(identity=Depends(drive_identity)):
        return native_state(identity)

    @app.post("/v1/native/start/setup")
    def setup_native_start(body: native_start.SetupInput, identity=Depends(drive_identity)):
        native_start.setup(db, settings, identity, body)
        return native_state(identity)

    @app.post("/v1/native/start/package")
    def native_package(identity=Depends(drive_identity)):
        return download_package(native_start.principal(db, identity, issuing=not bool(settings.assistant_public_origin)))

    @app.post("/v1/native/start/launch", status_code=201)
    def native_launch(identity=Depends(drive_identity)):
        return launch_package(native_start.principal(db, identity, issuing=not bool(settings.assistant_public_origin)))
