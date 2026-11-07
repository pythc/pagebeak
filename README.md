# 知序个人知识库

2026-11-06 已上线 0.13.9。用户选定范围后，WorkBuddy 主动扫描，更新任务进度并保存获准的本机资料概览。支持断点续扫，概览可在知识目录回读。测试范围和旧缓存兼容结果见 [本机资料概览](docs/workbuddy-host-documents-20261030.md)。

2026-11-05 接入修正：`0.13.8` 已上线并更新这台电脑的同名助理。已有有效连接直接进入初始化，不再要求新授权页或重复确认；宿主专用连接工具改为可选，首次或失效授权使用正常连接提示。原连接、文件与目录保留。验证及实际模型验收边界见 [修正记录](docs/有效连接与初始化修正-20261105.md)。

2026-11-05 最新上线：共享助理包 `0.13.7`，简化 WorkBuddy 安装请求、主动发起原生连接授权，去掉普通用户设置，补齐项目介绍和 15 条可搜索问答。[开始使用](https://zhixu.110-lab.cn/start) · [使用帮助](https://zhixu.110-lab.cn/help) · [改动及上线验证](docs/开始页与使用帮助-20261105.md)。当前校园线上运行 WeKnora + WorkBuddy 模式；下文其他版本说明保留其各自历史与本机范围。页面、协议和数据保留检查通过，真实模型自动弹出授权页尚未实测。

2026-11-05 后续更新：`0.13.6-identity` 已上线，保留现有会话、资料与客户端授权，不要求教师重装或重新登录。当前为兼容模式，真实外部账号复用尚缺平台应用凭据，未强制启用。发布经过一次自动回退，随后热更新成功；完整异常与验收见 [线上切换记录](docs/通用身份线上切换-20261105.md)。

2026-11-05：新增稳定外部账号身份、通用 MCP 客户端授权和旧空间显式绑定，支持 OIDC 与 WorkBuddy OAuth 适配。代码已通过本地验证并发布兼容版本；没有真实开发者应用凭据，外部账号必需模式及平台账号联调待完成。见 [身份与授权改造](docs/通用身份与客户端授权-20261105.md)。

2026-11-05：校园服务已重建到新虚拟机，域名保持 https://zhixu.110-lab.cn/start；旧服务器上的本项目服务和资料已按用户授权删除。当前部署、验证结果与证书续期边界见 [新虚拟机部署记录](docs/新虚拟机部署-20261105.md)。下文保留各本机引擎模式的运行说明。

2026-10-30：新增 WeKnora + WorkBuddy 外部整理模式，OCR 强制使用服务端 CPU。该模式已通过本机工程验收，默认正式配置与旧资料尚未切换；部署与迁移边界见 [改造说明](docs/weknora-workbuddy-20261030.md)，测试证据见 [验收结果](outputs/weknora-assistant-20261030/acceptance-report.json)。下文腾讯模式的运行方式继续适用于未迁移的原环境。

多用户文件空间与个人记忆库。当前版本以 **腾讯 Agent Memory 保存知识页和跨会话记忆，WorkBuddy 当前模型负责理解、整理、检索与填写**。

用户入口为 [文件空间](http://localhost:5878/)、[开始使用](http://localhost:5878/start) 和 [知识目录](http://localhost:5878/knowledge)。用户与 Agent 读取同一目录。首次使用见 [新手教程](http://localhost:5878/zhixu-guide.html)（[本地文档](docs/新手使用教程.md)）。

## 当前实现

- 保留账户隔离、Cloudreve 文件管理、原件上传、Office/扫描件解析与原文定位、在线预览、空间配额。
- 知识页使用腾讯 MemoryKnowledge，偏好和任务记忆使用 MemoryCore 原生存储与索引。
- WorkBuddy 通过 35 个 MCP 工具读原文、生成并更新知识页，无独立模型 API，不读取 WorkBuddy 登录凭据。
- 校园共享助理预览包 0.9.0：普通专家 + Skills + 27 个远程 MCP 工具，通过登录本人文件空间授权；不分发教师令牌或服务器磁盘路径。尚未发布市场，真实 WorkBuddy OAuth 与校园部署仍待验收，见 [试点实现与交付边界](docs/teacher-assistant-20261026.md)。原本机 35 工具接入继续保留。
- 可选本机文档索引：初始化选择具体目录或个人主目录内全部受支持文档；本机解析、中文检索、变化历史、重要原件按授权入库。文件清单可选择同步至知识目录。
- 新增原文锚点、确定性统计/金额换算、受控事迹生成与网页/Word 回读校验；未恢复旧候选事实引擎。
- 支持跨会话回读、来源版本失效、人工修改保护、幂等写入、并发版本检查、遗忘与到期清理。
- 原规则目录、候选事实采纳、字段映射和表单引擎已退出当前业务入口；旧表仅保留用于历史追溯和显式回滚。

**WorkBuddy 不运行时，后台只解析和同步原资料，语义整理等待 Agent。** 当前是本机外部 Agent 执行模式，不是腾讯完整自动提炼流水线，也没有无人值守模型服务。

最新依据、实现、迁移与边界见 [腾讯 Memory 切换方案](docs/tencent-memory-cutover.md)，历史切换验证见 [验收记录](docs/tencent-workbuddy-acceptance.md)，最新工程结果及同资料 A/B 状态见 [校验与恢复加固](docs/memory-verification-20261025.md)。旧技术方案和历史盲测不能作为当前版本的能力证明。

## 本机运行

需要项目现有 Python 3.12 环境、Docker/PostgreSQL 和 Cloudreve。运行时准备脚本支持 macOS arm64、Linux x86_64；校园 Linux 镜像的实际验证状态见上述试点文档。教师通过远程助理使用已上传资料时无需安装服务端运行时。本机索引的 Windows 兼容性尚未验收。

```bash
cd /Users/alex/workspace/agent-knowledge-base
uv sync --locked
docker compose --profile cloudreve up -d
.venv/bin/python scripts/setup_tencent_memory.py
.venv/bin/alembic upgrade head
# .env 中 AKB_KNOWLEDGE_ENGINE=tencent
.venv/bin/python scripts/zhixu.py start
```

已部署的这台电脑也可双击根目录 `启动知序.command`；先确保 Docker Desktop 已运行。状态、正常停止与重启分别使用 `scripts/zhixu.py status`、`stop`、`restart`（由项目 Python 执行）。后台进程和日志在忽略提交的 `data/runtime/`，未设置登录自启动。开发调试仍可单独运行 `scripts/dev.py`，不要同时启动两套。

启动器等待容器健康并核对数据库版本后再启动知识服务；迁移需显式执行，版本不符时停止启动，不自动降级。PostgreSQL 随 Docker 恢复，Worker 在短时数据库中断后退避重试。2026-10-26 的实际恢复验证见 [故障记录](docs/service-recovery-20261026.md)。

首次全新环境仍需运行 `scripts/setup_local.py`、`scripts/bootstrap.py` 和 `scripts/setup_cloudreve.py` 配置基础服务；已有环境不要覆盖配置。腾讯私有数据保存在 `data/tencent-memory/`，服务密钥在忽略提交的 `secrets/tencent-memory.key`，两项服务仅监听回环 8420、8421。

```bash
# 静态检查与回归
.venv/bin/ruff check backend
.venv/bin/pytest
# 需要真实腾讯服务
AKB_TEST_TENCENT=1 .venv/bin/pytest backend/tests/test_tencent_memory.py backend/tests/test_memory_verification.py
.venv/bin/python scripts/smoke_tencent_memory.py
# 原生文件前端
.venv/bin/python scripts/build_cloudreve_frontend.py --deploy
```

新接入包 0.8.0 包含 [腾讯版管家技能](integrations/workbuddy/tencent-skills/personal-knowledge/SKILL.md)。使用 WorkBuddy 正常安装、信任与加载流程，在当前对话完成初始化；正式市场插件尚未发布。本机索引设计、范围及真实 WorkBuddy 两阶段验证见 [本机资料接入](docs/local-documents-20261026.md)。

所有本地盲测人物、工作资料及表单均为合成数据。真实用户数据、密钥、模型输出和临时资产不提交仓库。文件管理定制方式见 [维护说明](integrations/cloudreve/README.md)。
