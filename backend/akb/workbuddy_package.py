"""Build a private local WorkBuddy expert: Skill, pinned MCP dependency and interview entry."""

import io
import json
import sys
from datetime import datetime
from urllib.parse import urlencode
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from akb.config import ROOT

PACKAGE_VERSION = "0.8.0"
SKILL_ROOT = ROOT / "integrations/workbuddy/skills/personal-knowledge"
TENCENT_SKILL_ROOT = ROOT / "integrations/workbuddy/tencent-skills/personal-knowledge"
WEKNORA_SKILL_ROOT = ROOT / "integrations/workbuddy/weknora-skills/personal-knowledge"

def engine_skill_root(engine):
    return WEKNORA_SKILL_ROOT if engine == "weknora" else TENCENT_SKILL_ROOT if engine == "tencent" else SKILL_ROOT



def package_identity(knowledge_base_id: str):
    kb = str(UUID(knowledge_base_id))
    return "zhixu-" + kb + "-expert"


def initialization_prompt(knowledge_base_id: str, skill_version: str):
    kb = str(UUID(knowledge_base_id))
    name = package_identity(kb)
    return (f"请按随包的 personal-knowledge 知识库管家技能开始或继续初始化。只使用 {name}，"
            f"先调用 get_knowledge_context(expected_knowledge_base_id=\"{kb}\") 核对身份，"
            f"再调用 start_onboarding(skill_version=\"{skill_version}\") 和 read_onboarding，"
            "就在当前对话继续，保留已有回答，结合上下文合并询问缺少的信息并批量保存；"
            "具备信息时直接完成初始化并整理资料，不要求回网页点击初始化，不从空间名推断我的身份。"
            "同时让我选择是否检索本机文档：可不启用、选择目录，或检索个人主目录中的全部受支持文档；"
            "说明扫描范围、排除项和片段处理方式，按我的选择启用本地索引。自动收录重要原件与共享目录分别按授权设置。")


def initialization_link(knowledge_base_id: str, skill_version: str):
    # Native task entry supports expertId and prepares the prompt; the host retains Send and authorization.
    prompt = initialization_prompt(knowledge_base_id, skill_version)
    prompt += "连接未就绪则按宿主的实际提示处理；不能换用其他库、直接 HTTP、修改信任记录或重复安装。"
    prompt += "核验正常时直接用日常中文了解我的需求并推进任务，不展示库 ID、工具名、修订号或调试状态。"
    return "workbuddy://task?" + urlencode({
        "action": "start", "expertId": package_identity(knowledge_base_id), "prompt": prompt,
        "cwd": str(ROOT), "permissionMode": "default"})


def build_package(settings, knowledge_base_id: str, code: str, expires: datetime, skill_version: str):
    kb = str(UUID(knowledge_base_id))
    name = package_identity(kb)
    initial = initialization_prompt(kb, skill_version)
    first_prompt = {"zh": initial, "en": initial}
    manifest = {
        "name": name, "version": "0.11.0" if settings.knowledge_engine == "weknora" else PACKAGE_VERSION,
        "description": "Initialize and maintain an evidence-backed personal knowledge library.",
        "author": {"name": "知序"}, "expertType": "agent", "agentName": "knowledge-steward",
        "agents": ["./agents/knowledge-steward.md"], "skills": ["./skills/personal-knowledge"],
        "displayName": {"zh": "知序知识库管家", "en": "Zhixu Knowledge Steward"},
        "profession": {"zh": "个人知识库管家", "en": "Personal Knowledge Steward"},
        "displayDescription": {
            "zh": "帮助你建立和持续维护个人知识库，整理办公资料、更新共享目录、查找原文依据并准备个人信息填写草稿。",
            "en": "Organize personal documents, maintain a shared catalog and prepare evidence-backed drafts.",
        },
        "avatar": "avatars/zhixu.svg", "categoryId": "04-DataAI", "plugin": name,
        "defaultInitPrompt": first_prompt,
        "tags": [{"zh": zh, "en": en} for zh, en in [
            ("资料整理", "Organization"), ("知识检索", "Retrieval"), ("辅助填表", "Form drafts")]],
        "quickPrompts": [first_prompt,
                         {"zh": "整理我的资料目录，保留人工修改", "en": "Organize my catalog and preserve my edits"},
                         {"zh": "根据资料准备一份有依据的填表草稿", "en": "Prepare an evidence-backed form draft"}],
    }
    config = {"mcpServers": {name: {
        "type": "stdio", "command": sys.executable, "cwd": str(ROOT),
        "args": ["-m", "akb.workbuddy_entry"],
        "env": {"AKB_API_URL": f"http://127.0.0.1:{settings.api_port}",
                "AKB_EXPECTED_KB_ID": kb, "AKB_JOIN_CODE": code},
        "x-workbuddy": {
            "displayName": {"zh": "知序个人知识库", "en": "Zhixu Personal Knowledge"},
            "description": {"zh": "仅连接当前授权的个人资料库", "en": "Access only the pinned personal library"},
            "icon": "./avatars/zhixu.svg",
        },
    }}}
    agent = ("---\nname: knowledge-steward\n"
             "description: 初始化和维护当前授权的知序个人知识库，检索资料并准备有依据的填写草稿。\n---\n\n"
             "你是知序知识库管家。执行本包 skills/personal-knowledge/SKILL.md，初始化时读取其中的 references/onboarding.md。\n\n"
             f"本包仅授权知识库 `{kb}`，连接名称 `{name}`。每次先用 expected_knowledge_base_id 核对身份。\n\n"
             "安装就绪后直接在当前对话执行初始化技能，优先恢复已有进度；相关信息可合并询问、批量保存。"
             "不要让用户回网页点击初始化、另开对话或再次召唤专家来启动流程。"
             "MCP 依赖由 WorkBuddy 安装与授权，不运行旧安装脚本、不改宿主配置或信任记录。"
             "连接未就绪则按宿主提示处理，不能换用其他库或直接 HTTP 假装调用成功。"
             "不从登录名或空间名称推断身份，不替用户回答；可用 accept_knowledge_facts 自主采纳有原文依据的候选，"
             "明确标记 Agent 采纳，不冒充用户本人确认，不覆盖人工决定，不自行删除或对外提交。\n")
    if settings.knowledge_engine in {"tencent", "weknora"}:
        agent = agent.replace("可用 accept_knowledge_facts 自主采纳有原文依据的候选，明确标记 Agent 采纳，",
                              "由当前 WorkBuddy 模型阅读原文并用 save_library_item 写入腾讯 Memory，")
    if settings.knowledge_engine == "weknora":
        agent = agent.replace("腾讯 Memory", "WeKnora，按技能领取整理任务并完成回写")
    files = {
        ".codebuddy-plugin/plugin.json": json.dumps(manifest, ensure_ascii=False, indent=2),
        ".mcp.json": json.dumps(config, ensure_ascii=False, indent=2),
        "agents/knowledge-steward.md": agent,
        "README.md": ("# 知序本机接入包\n\n这是同一台电脑上的开发验收包，不是已发布市场资产。\n\n"
                             f"绑定知识库：{kb}\n首次连接码有效至：{expires.isoformat()}\n\n"
                             "包含短期一次性连接码，请勿分享或上传开放平台。首次 MCP 启动时才兑换；"
                             "长期凭据由连接程序保存在本机私有目录，不写入包或技能。首次成功后可跨进程复用。\n\n"
                             "在 WorkBuddy 的专家导入入口导入本包，按宿主提示连接 MCP，由当前对话直接读取技能并完成初始化。"
                             "若该版本没有本地导入入口，保留包并使用本项目本地安装验证脚本；不要覆盖宿主配置。\n\n"
                             "正式发布还需开发者账号、真实作者联系信息、远程 HTTPS MCP 和用户授权流程；"
                             "不能把这个带本机路径及短期码的私有包直接发布。\n"),
    }
    files["avatars/zhixu.svg"] = (ROOT / "integrations/cloudreve/overlay/public/static/img/zhixu-mark-v1.svg").read_text()
    skill_root = engine_skill_root(settings.knowledge_engine)
    for path in sorted(skill_root.rglob("*")):
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
            files["skills/personal-knowledge/" + path.relative_to(skill_root).as_posix()] = path.read_bytes()
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path, text in files.items():
            info = ZipInfo(name + "/" + path)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, text.encode() if isinstance(text, str) else text)
    return output.getvalue(), name


def published_install_link(expert_name: str, share_code: str):
    """Only for an actual published WorkBuddy asset; never encode credentials or pretend it is published."""
    if not expert_name.strip() or not share_code.strip():
        raise ValueError("需要已发布专家的名称与分享码")
    return "workbuddy://codebuddy-ide/expert/install?" + urlencode(
        {"expertname": expert_name, "sharecode": share_code})
