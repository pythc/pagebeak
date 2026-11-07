"""User-independent expert/connector artifacts. Never distribute local tickets."""
import hashlib
import io
import json
import re
from urllib.parse import urlencode
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from akb.assistant_auth import public_origin
from akb.config import ROOT

NAME, VERSION = "zhixu-teacher-assistant", "0.9.0"
WEKNORA_VERSION = "0.13.9"
INITIAL = "请帮我开始使用知序。先让我选择检索全部文件、指定文件夹或暂不检索，再按我的选择建立索引并帮助我使用资料。"


def connection_name(origin):
    """Keep independently hosted services out of a same-name MCP cache."""
    canonical = public_origin(origin)
    return NAME + "-" + hashlib.sha256(canonical.encode()).hexdigest()[:12]


def package_version(engine):
    return WEKNORA_VERSION if engine == "weknora" else VERSION


def published_package(settings):
    """A shared artifact has no teacher identifier, credential or server-local path."""
    if not settings.assistant_public_origin or not settings.assistant_author_email:
        raise ValueError("管理员尚未配置共享助理的服务地址和发布者邮箱")
    return build_assistant(settings.assistant_public_origin, settings.assistant_author_email,
        (ROOT / "integrations/workbuddy/assets/zhixu-512.png").read_bytes(), settings.knowledge_engine)[0]


def installation(settings, knowledge_base_id):
    import hashlib

    content = published_package(settings)
    sha256 = hashlib.sha256(content).hexdigest()
    url = public_origin(settings.assistant_public_origin) + "/v1/assistant/package"
    if knowledge_base_id:
        UUID(knowledge_base_id)
    prompt = (f"请使用 expert-manager 下载并原样安装知序助理包：\n{url}\n"
              "装好后按技能开始初始化；已有有效连接直接继续，未连接时发起正常授权。")
    return {"package_url": url, "sha256": sha256, "package_version": package_version(settings.knowledge_engine),
        "prompt": prompt, "launch_url": "workbuddy://task?" + urlencode({"action": "start", "permissionMode": "default", "prompt": prompt})}


def initialization_link(knowledge_base_id, origin):
    kb = str(UUID(knowledge_base_id))
    connection = connection_name(origin)
    return "workbuddy://task?" + urlencode({"action": "start", "expertId": NAME, "permissionMode": "default",
        "prompt": f"请使用知序教师助理的 {connection} 连接，先核对服务 {public_origin(origin)} 和知识库 {kb} 后在当前对话继续初始化和整理资料。已有信息不重复询问，缺项可以一起回答。"})


def skill_texts(engine="tencent", origin=None):
    from akb.workbuddy_package import engine_skill_root

    root = engine_skill_root(engine)
    texts = {p.relative_to(root).as_posix(): p.read_text()
             for p in root.rglob("*.md") if p.is_file() and not p.is_symlink()}
    texts["SKILL.md"] = texts["SKILL.md"].replace(
        "1. 按本次专家声明的完整连接名/知识库 ID 发现工具，再用 `get_knowledge_context(expected_knowledge_base_id=...)` 核对绑定。",
        "1. 只使用本专家声明的 zhixu-teacher-assistant 连接。首次调用 get_knowledge_context 获取当前已授权的库 ID，在本会话固定；后续携带 expected_knowledge_base_id 核对。")
    texts["SKILL.md"] = texts["SKILL.md"].replace("`tencent-workbuddy-v5`", "`tencent-workbuddy-v5-remote`")
    texts["SKILL.md"] = texts["SKILL.md"].replace(
        "原文件使用 `prepare_file_upload` 获取暂存目录，由宿主按正常文件权限复制用户指定文件，再 `upload_file` 上传；",
        "远程连接使用 get_upload_entry 打开文件空间上传原文件；它无法读取教师电脑路径。本机连接可使用 prepare_file_upload / upload_file；")
    texts["SKILL.md"] += ("\n## 本共享助理的连接范围\n\n"
        "这是普通个人 WorkBuddy 账号使用的专家，不需要 Buddy 应用或企业席位。模型消耗属于教师当前登录的 WorkBuddy 账号。"
        "连接认证由宿主打开知序，个人空间自动开通，用户明确允许后完成；不要求教师注册、登录或手动关联，不向模型提供文件空间密码、授权码或令牌。"
        "需要更新说明时调用 read_assistant_skill，不读取服务器返回的磁盘路径。"
        "后台原文解析可以继续，WorkBuddy 未运行时语义整理等待下次对话。\n")
    texts["SKILL.md"] += ("\n先按 references/connection.md 检查现有连接。现有连接通过校验就直接继续初始化，"
        "不以打开新授权页为前置条件，不再询问是否使用该连接继续；首次连接或明确授权失效才发起授权。\n")
    texts["references/connection.md"] = (
        "# 连接个人资料空间\n\n"
        "## 先检查现有连接\n\n"
        f"只使用本助理声明的完整连接名 {NAME}。先发现并实际调用 get_knowledge_context，"
        "传入本助理声明的 expected_service_origin；已有明确库 ID 时同时传 expected_knowledge_base_id。"
        "成功返回后核对 service_origin、当前用户和库 ID，并在会话内固定库 ID。"
        "工具出现在列表里不等于调用成功；地址、用户或库不符时停止资料操作，不切换其他连接补做。\n\n"
        "现有连接校验成功就视为已连接，直接读取最新技能和 references/onboarding.md，"
        "按 start_onboarding / read_onboarding 继续。无需生成新授权页，也不再询问“是否使用现有连接继续”。"
        "本次没有重新授权并非初始化未完成的理由。可选的宿主连接工具不存在也不影响已经生效的连接。"
        "用户尚未选择本机范围时，直接提供检索全部文件、指定文件夹、暂不检索三个选项；选择前不扫描。"
        "已有明确选择或进度就恢复，不重置初始化、不重复提问。\n\n"
        "## 未连接或授权明确失效时\n\n"
        "只有连接未加载、未授权或明确返回 401 / invalid_grant 等授权失效信号时，才发起宿主正常 OAuth。"
        "403 权限不足、网络超时、服务错误、地址或库不符不能当作授权过期，更不能因此新建替代空间。"
        "workbuddy_request_mcp_connection 是可选宿主能力，不保证每个版本或会话都提供。"
        "可用时发现后调用，servers 只填上文完整连接名；检索明确不可用时，不再尝试直接调用不存在的工具。"
        "也可使用宿主已经提供的连接/重新连接卡片；由客户端生成并打开当前授权页，用户确认后继续原操作。"
        "宿主若明确返回当前请求的授权 URL，可用浏览器打开它。"
        "只有卡片可用时，简短请用户点击该卡片，不把必须亲自确认的步骤说成全自动。"
        "均不可用时只说明当前需要在 MCP 服务管理中连接知序个人资料空间，不反复让用户重装或确认继续。"
        "工具返回 connected 或客户端确认连接成功后重试一次原步骤，failed 或 skipped 时本轮停止重试。\n\n"
        "不复用历史 assistant_request，不拼造授权链接，不读取客户端的全局配置或凭据，"
        "不另造 OAuth 客户端获取令牌来冒充 WorkBuddy 已连接。保留人工修改，不因重装或重连重新建立资料库。\n")
    if engine != "weknora":
        texts["SKILL.md"] += "当前远程包不含本机索引。先继续使用已上传资料，不扫描服务器冒充教师电脑。\n"
    if engine == "weknora":
        texts["SKILL.md"] = texts["SKILL.md"].replace("`weknora-workbuddy-v1`", "`weknora-workbuddy-v1-remote`")
    if origin:
        texts = {name: text.replace(NAME, connection_name(origin)) for name, text in texts.items()}
    return texts


def skill_files(engine="tencent", origin=None):
    from akb.workbuddy_package import engine_skill_root

    files = dict(skill_texts(engine, origin))
    if engine == "weknora":
        root = engine_skill_root(engine)
        for p in (root / "scripts").rglob("*"):
            if p.is_file() and not p.is_symlink() and p.suffix in {".py", ".zip", ".json"}:
                files[p.relative_to(root).as_posix()] = p.read_bytes()
    return files


def archive(files):
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as bundle:
        for path, content in sorted(files.items()):
            info = ZipInfo(NAME + "/" + path)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, content.encode() if isinstance(content, str) else content)
    return output.getvalue()


def build_assistant(origin, author_email, avatar_png, engine="tencent"):
    origin = public_origin(origin)
    connection = connection_name(origin)
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", author_email):
        raise ValueError("正式发布包需要开发者提供的作者邮箱")
    if not avatar_png.startswith(b"\x89PNG\r\n\x1a\n") or len(avatar_png) > 500 * 1024:
        raise ValueError("头像必须是小于 500 KiB 的实际 PNG")
    import struct

    if struct.unpack(">II", avatar_png[16:24]) != (512, 512):
        raise ValueError("专家头像须为 512×512 PNG")
    first = {"zh": INITIAL, "en": "Connect my document space, initialize here and help me use my first document."}
    manifest = {"name": NAME, "version": VERSION, "expertType": "agent", "agentName": "teacher-assistant",
        "description": "Maintain teachers' personal documents and prepare evidence-backed forms and materials.",
        "author": {"name": "知序", "email": author_email}, "agents": ["./agents/teacher-assistant.md"],
        "skills": ["./skills/personal-knowledge"], "plugin": NAME,
        "dependencies": {"mcpServers": "./.mcp.json"},
        "displayName": {"zh": "知序教师助理", "en": "Zhixu Teacher Assistant"},
        "profession": {"zh": "教师资料助理", "en": "Teacher Document Assistant"},
        "displayDescription": {"zh": "整理教师个人、教学与科研资料，维护可追溯的知识目录，查找原文依据，辅助填写表格与撰写工作材料。",
            "en": "Organize teaching and research documents, maintain a traceable catalog and prepare forms and materials."},
        "avatar": "avatars/zhixu.png", "categoryId": "04-DataAI", "defaultInitPrompt": first,
        "tags": [{"zh": z, "en": e} for z, e in [("资料整理", "Documents"), ("辅助填表", "Forms"), ("材料撰写", "Writing")]],
        "quickPrompts": [first, {"zh": "整理我的资料目录，保留人工修改", "en": "Organize my catalog and preserve my edits"},
                         {"zh": "根据原文依据帮我填写表格或撰写材料", "en": "Prepare a form or material with original evidence"}]}
    remote = {"type": "streamableHttp", "url": origin + "/mcp", "x-workbuddy": {
        "displayName": {"zh": "知序个人资料空间", "en": "Zhixu Personal Documents"},
        "description": {"zh": "自动开通个人资料空间；模型费用由当前 WorkBuddy 账号承担", "en": "Connect your personal document space"},
        "icon": "./avatars/zhixu.png", "auth": {"type": "oauth"}}}
    agent = ("---\nname: teacher-assistant\ndescription: Maintain a teacher's documents, catalog and evidence-backed forms.\n"
             "displayName:\n  zh: 知序教师助理\n  en: Zhixu Teacher Assistant\n"
             "profession:\n  zh: 教师资料助理\n  en: Teacher Document Assistant\nskills:\n  - personal-knowledge\n---\n\n"
             f"执行随包 personal-knowledge 技能，只使用 {connection} 连接。"
             f"任何其他资料操作前必须调用 get_knowledge_context(expected_service_origin='{origin}')，并检查返回 service_origin 与声明地址完全一致；缺少参数支持、缺字段或地址不符即停止并核对连接来源，不能继续使用同名旧连接。"
             "现有连接校验成功就直接读取最新技能、start_onboarding / read_onboarding 并继续初始化，不要求重新授权，不再询问是否继续。"
             "按 references/connection.md 处理缺失或失效连接。workbuddy_request_mcp_connection 仅为可选宿主能力，不是初始化前置条件；"
             "它不可用时不直接调用、不因而阻断有效连接。首次或失效连接使用宿主正常连接卡片授权，未实际完成不伪造成功。"
             "第一次对话先提供检索全部文件、指定文件夹、暂不检索三个选项；等待用户选择后再扫描。不先盘问姓名职业，不要求网页初始化。已有明确授权不重复问。"
             "先固定当前用户和库 ID，再恢复腾讯 Memory 中的目录、明确偏好和任务。"
             "使用本人 WorkBuddy 模型，不索取模型密钥；保留人工修改，填写后实际保存回读并核验。\n")
    if engine == "weknora":
        agent = agent.replace("腾讯 Memory", "WeKnora").replace("恢复", "恢复并按技能领取整理任务，查看")
        agent += ("首次初始化前读 references/onboarding.md。用户选定范围后主动扫描，按 references/local-overview.md "
                  "更新任务记忆并保存、回读获准的本机资料概览。\n")
        manifest["version"] = WEKNORA_VERSION
    files = {".codebuddy-plugin/plugin.json": json.dumps(manifest, ensure_ascii=False, indent=2),
        ".mcp.json": json.dumps({"mcpServers": {connection: remote}}, ensure_ascii=False, indent=2),
        "agents/teacher-assistant.md": agent, "avatars/zhixu.png": avatar_png,
        "README.md": "# 知序教师助理\n\n公共包不含教师身份、知识库编号、接入码或令牌。通过 WorkBuddy 正常授权连接本人空间。\n"
        "普通个人账号可使用的市场分发仍须实际审核和验收；生成包不表示已发布。\n"
        "云端资料由 MCP 检索；WeKnora 版随 Skill 提供本机索引脚本，使用 WorkBuddy 运行环境，无需额外客户端。首次使用须核对宿主能力并由用户选择目录。\n"}
    files.update({"skills/personal-knowledge/" + name: value for name, value in skill_files(engine, origin).items()})
    expert = archive(files)
    connector = {"connector-meta.json": json.dumps({"source": NAME, "name": "知序教师助理", "name_zh": "知序教师助理",
        "name_en": "Zhixu Teacher Assistant", "type": "mcp", "minWorkbuddyVersion": "5.0.0",
        "description": "Connect personal documents and prepare evidence-backed forms with the current WorkBuddy model.",
        "description_zh": "连接本人资料空间，使用当前 WorkBuddy 模型整理资料和填写表格",
        "description_en": "Connect personal documents and prepare evidence-backed forms with the current WorkBuddy model.",
        "examples_zh": ["帮我整理个人资料目录", "根据资料帮我填写表格"],
        "examples_en": ["Organize my document catalog", "Fill a form using my documents"], "version": package_version(engine),
        "author": {"name": "知序", "email": author_email}}, ensure_ascii=False, indent=2),
        "mcp.json": json.dumps({"mcpServers": {connection: {k: v for k, v in remote.items() if k != "x-workbuddy"}}}, indent=2),
        "icon.svg": (ROOT / "integrations/cloudreve/overlay/public/static/img/zhixu-mark-v1.svg").read_text()}
    connector.update({"skills/personal-knowledge/" + name: value for name, value in skill_files(engine, origin).items()})
    return expert, archive(connector)
