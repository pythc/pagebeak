# WorkBuddy MCP 接入

日期：2026-10-17。用户已确认：在 WorkBuddy 内调用知识库、整理资料并准备表单。

当前服务共 37 个工具：新增 accept_knowledge_facts，允许 Agent 核验原文后自主采纳结构化候选，标记为 Agent 采纳并保留人工决定；此前四个文件工具支持授权原件上传。删除仍须用户在网页批准具体清单，MCP 不能自行批准。旧版截图中的工具数量是历史结果。真实 stdio 协议验收与 WorkBuddy 客户端验收分别记录；更新服务后需重连 MCP 以重新加载工具。首次使用优先从 [开始引导](onboarding.md) 进入；手动配置保留为高级入口。详见 [Agent 采纳](agent-adoption.md) 和 [原文件上传](mcp-file-upload.md)。

## 接入关系

WorkBuddy 负责理解任务和使用其模型；知识库负责保存资料、执行检索、维护事实及约束访问。连接为 `WorkBuddy → 本机 stdio MCP 进程 → 个人知识库 API → PostgreSQL / 原件存储`。

本实现不将 WorkBuddy 登录态转换为后端模型凭据。官方支持本地 stdio 和自填 Token；云任务 Open API 是另一条需开发者应用及 OAuth 的路线。[官方连接器说明](https://open.workbuddy.cn/docs/connector)、[官方 Open API](https://open.workbuddy.cn/docs/openapi)

## 配置

1. 启动服务，打开 `http://localhost:5876/` 并登录自己的账号。
2. 在“连接 WorkBuddy”中选择权限，生成个人令牌。有效期 30 天，仅显示一次。
3. 打开 WorkBuddy 的「专家·技能·连接器 → 连接器 → 自定义连接器 → 添加 MCP」，将 `personal-knowledge` 合并到现有 `mcpServers`，使用 stdio。编辑器对应完整 `~/.workbuddy/mcp.json`，不能覆盖其他已有服务。
4. 连接后说：“列出我的知识库资料，并检查处理状态。”

首次连接需信任本地编写的服务。实际验收时已看到 `personal-knowledge 10/10 个工具已启用`。如果客户端升级后自动化控制仍保留旧窗口状态，应重新建立控制连接；不要对 React 富文本输入区直接设置底层文本值。

可运行 `uv run python scripts/workbuddy_config.py`，在 `work/workbuddy-mcp.json` 生成带实际 Python 路径的无凭据模板。

```json
{
  "mcpServers": {
    "personal-knowledge": {
      "type": "stdio",
      "command": "/absolute/path/to/agent-knowledge-base/.venv/bin/python",
      "args": ["-m", "akb.mcp_server"],
      "env": {
        "AKB_API_URL": "http://127.0.0.1:5876",
        "AKB_TOKEN": "REPLACE_WITH_YOUR_PERSONAL_TOKEN",
        "AKB_EXPECTED_KB_ID": "REPLACE_WITH_KNOWLEDGE_BASE_ID_FROM_WEB_CONFIG"
      }
    }
  }
}
```

拥有 knowledge:read、knowledge:write、profile:read 的连接可采纳结构化候选，记录为 Agent 操作；不能创建用户独立声明或覆盖人工决定。令牌不能批准删除或创建其他令牌；清理只能执行网页已批准的具体清单。原生文件的跨系统彻底删除尚不支持。不要提交包含真实令牌的配置。本机允许回环 HTTP；远程服务必须使用 HTTPS，并另行完成部署验证。

网页“连接 WorkBuddy”提供本人知识库 ID。已有配置必须补充 `AKB_EXPECTED_KB_ID` 后重连；未固定身份时，只允许 `get_knowledge_context` 发现连接身份，其余数据操作停止。指定库不匹配返回错误，不自动尝试其他连接。本机已有 personal-knowledge 与 persona-acceptance 配置已核对各自凭据后补充 ID，客户端是否已加载新配置仍需重连核验。

## 工具

| 工具 | 用途 | 权限 |
| --- | --- | --- |
| get_knowledge_context | 核验当前知识库身份；任务可以额外指定 expected_knowledge_base_id | 有效身份 |
| read_knowledge | 分页读取同一份结构化知识、待处理项与 Markdown | knowledge:read + profile:read |
| list_knowledge_tasks | 查看等待或执行中的当前原文版本整理任务 | knowledge:read |
| claim_knowledge_task | 领取/续租任务，取得版本与字段约束 | knowledge:read + knowledge:write |
| submit_knowledge_task | 提交带当前证据的候选，校验租约、版本与幂等标识 | knowledge:read + knowledge:write |
| accept_knowledge_facts | 批量采纳核验后的候选；记录 Agent、版本与理由，保留人工决定 | knowledge:read + knowledge:write + profile:read |
| read_form_draft | 填写前重读草稿；资料变化后旧答案不返回 | forms:write |
| read_catalog | 分页读取与网页共用的目录、Markdown、人工修改及原文定位 | knowledge:read；个人介绍另需 profile:read |
| organize_catalog | 请求后台增量整理，保留人工修改，返回任务 ID | knowledge:write |
| list_sources | 分页查看资料与状态 | knowledge:read |
| search_knowledge | 关键词查找原文证据 | knowledge:read |
| read_source | 分页读取资料证据 | knowledge:read |
| read_evidence | 读取原文及位置 | knowledge:read |
| get_profile | 读取个人事实与冲突 | profile:read |
| import_text | 保存用户要求导入的原始文字 | knowledge:write |
| prepare_file_upload | 核验目标、容量及限制，准备本库私有本机收件目录 | knowledge:read |
| upload_file | 上传宿主按授权复制的原件；分块、哈希校验、重试续传 | knowledge:read + knowledge:write |
| read_file_upload | 查看已接收字节和上传状态 | knowledge:write |
| cancel_file_upload | 取消未完成上传并释放预留，不删除已入库原件 | knowledge:write |
| get_job | 查看后台任务 | knowledge:read |
| propose_profile_facts | 根据原文提出待确认事实 | knowledge:write |
| save_knowledge_page | 保存有依据的生成专题 | knowledge:write |
| prepare_form | 准备有依据的表单草稿 | forms:write |
| get_sync_state | 当前、索引、Agent 确认哈希及扫描状态 | knowledge:read |
| read_file_changes | 按游标读取文件变动历史 | knowledge:read |
| read_file_change_content | 读取变更前后已解析文本，旧版本标记 historical | knowledge:read |
| sync_file_changes | 请求扫描变更并整理目录 | knowledge:write |
| acknowledge_file_changes | 校验版本并推进本令牌确认检查点 | knowledge:read |

资料 ID 不决定权限，服务端根据令牌确定用户。结构化事实区分 Agent 采纳与用户采纳，网页可修正或撤销；生成页面不成为独立事实依据；草稿不会自动提交第三方网站。旧六字段 Claim 的人工决定接口仍保留网页限制，不与结构化采纳混用。

建议同时使用 `integrations/workbuddy/skills/personal-knowledge/SKILL.md`，指导 Agent 使用证据、处理未知和保留授权边界。

语义整理详细流程见 [semantic-tasks](../integrations/workbuddy/skills/personal-knowledge/references/semantic-tasks.md)。有明确依据的候选可由 Agent 采纳，模糊内容保持待处理；Agent 的“已读全部证据”只是执行声明，服务端校验覆盖集合，不能证明模型理解正确。Word 占位模板由知识库网页直接下载填写版；没有占位符的任意 Word 仍需要宿主文档编辑能力。

## 验证边界

personal-knowledge Skill 已扩展为“知识库管家”，覆盖初始化提问、资料收录判断、日常维护和补充引导。Skill 与 MCP 连接分别加载；有工具连接不等于已安装或选用 Skill。完整行为与当前能力缺口见 [Skill 设计](knowledge-steward-skill-design.md)。后续真实管家会话已完成 Skill 加载、目标身份核验和首题提问，见 [一键接入记录](workbuddy-one-click.md)；这不代表三题访谈、所有技能行为和原件上传均已通过真实模型验收。

已通过 MCP Python SDK 的实际 stdio 握手、工具发现及对运行中 API 的调用。另已在本机 WorkBuddy 客户端配置连接，使用隔离合成账号完成真实模型调用、检索整理和本地浏览器填表，见 [实际验收记录](workbuddy-acceptance.md)。这是合成资料验收，不代表真实用户资料已接入。

`scripts/smoke_workbuddy.py` 只操作本地合成账号，检查后撤销测试令牌。它验证协议及服务行为，不模拟 WorkBuddy 模型的判断。

本轮真实客户端连接通过忽略提交的 `work/workbuddy-e2e/launch_mcp.py` 读取同目录 `session.json` 中的验收凭据；文件权限为 0600。WorkBuddy 配置仅含本地启动命令，不含令牌。该连接保留用于本机演示，受 30 天有效期限制；删除该实验目录前，先移除 WorkBuddy 的对应 MCP 配置并在知识库撤销令牌。不要把这一验收账号当作正式个人账号。

本地浏览器测试表单：

```bash
uv run python -m http.server 5880 --bind 127.0.0.1 --directory integrations/workbuddy/demo
```

打开 `http://127.0.0.1:5880/teacher-form.html`。页面不保存填写值，刷新后为空；“检查填写结果”只更新当前页面。已保存的 MCP 草稿和专题则保留在知识库数据库中。实际填表由 WorkBuddy 内置 `agent-browser` 完成；知识库连接器自身只准备草稿，不提交外部站点。
