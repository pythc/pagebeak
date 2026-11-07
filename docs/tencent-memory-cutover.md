# 腾讯 Memory + WorkBuddy：当前实现

更新日期：2026-10-25。最新增补和同资料 A/B 状态见 [校验与恢复加固](memory-verification-20261025.md)。本文替代旧技术方案中的规则目录、候选事实采纳、结构化字段映射与表单业务引擎；旧文档和数据仅作为历史、回滚资料保留。

## 产品与运行边界

用户仍在 Cloudreve 中文文件空间上传、管理和预览文件，在“开始使用”接入 WorkBuddy，在“知识目录”查看、修改知识页与记忆。用户和 Agent 使用同一目录，不维护第二份目录副本。

腾讯 Memory 是唯一的知识页和记忆正文存储、索引实现。当前 WorkBuddy 对话中的模型负责读原文、归纳、判断资料关系、写入知识页、记住偏好以及填表。没有向服务端暴露 WorkBuddy 订阅的模型 API，没有提取其登录凭据，也没有改用另一个 CLI 模型。

这是 **外部 Agent 执行模式**：WorkBuddy 关闭时，后台能解析和同步原资料，不能继续完成语义整理。状态明确为 `awaiting_agent`。未接入腾讯内部自动提炼调度器；不能把当前实现称为完整原生 L0→L3 自动提炼流水线。

## 组成与唯一数据来源

| 内容 | 实现与存放位置 |
|---|---|
| 原文件、账户、上传、配额、Office/OCR、原文定位 | 保留现有基础设施；原件在文件存储，解析片段与版本在 PostgreSQL |
| 知识页及索引 | 腾讯 MemoryKnowledge 原生 Wiki 写入、索引、搜索；本机 `data/tencent-memory/knowledge` |
| 偏好、任务记忆 | 腾讯 MemoryCore 原生 L0 对话增查搜删；本机 `data/tencent-memory/core` |
| 目录与生命周期 | PostgreSQL 仅保存条目 ID、标题、版本、来源版本、人工保护、到期、删除及远端引用；不保存第二份生成正文 |
| 推理 | WorkBuddy 当前对话模型通过 27 个 MCP 工具实际读取、生成、回写 |
| 用户界面 | `http://localhost:5878/knowledge`；旧 5876 根入口转到此页 |

MemoryCore 的 `scenario/write` 和 `atomic/update` 是更新接口，不能创建新记忆。本版使用真实可创建的原生 L0 CRUD，不把更新接口伪装成创建。MemoryKnowledge 的普通页面写入不会自动将 Wiki 置为可检索状态；小型宿主适配器调用原生写入、索引同步并验证 ready 后才确认发布。

## 上游及模型连接依据

- [TencentCloud/TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)，固定提交 `6a4bb55ea54834d8265ce52024d3e3c75db30d38`，MIT。归档、Node 与 SQLite 原生模块校验值见 `integrations/tencent-memory/upstream.json`。
- 使用原生 `StorePool`、`handleV2Route`、Wiki 服务与索引；宿主入口位于 `integrations/tencent-memory/`，未修改上游源文件。
- [WorkBuddy MCP 连接器](https://open.workbuddy.cn/docs/connector)是本版入口。其[任务 API](https://open.workbuddy.cn/docs/openapi)需要开发者应用授权，不等于可直接复用桌面订阅的通用模型 API，本版没有启用。
- 当前仅 BM25/全文检索，无独立 embedding API。原生中文分词对部分短专名可能漏召回：真实探针“青禾”未命中、同一内容“结论”命中，按 ID 回读完整。后续已用已登记原生正文的字面检索补充，当前“青禾”专项通过；这不等于新增 embedding 语义检索。

## 已实现的控制

- 当前租户由认证服务注入，调用者不能指定其他用户的腾讯隔离维度。两项私有服务仅监听回环 8420、8421，所有数据接口另有服务密钥认证。
- 新写入用 request_id 幂等；更新使用 expected_revision，冲突返回 409。生成正文使用不可变远端修订，只有正式登记的修订参与召回。
- 依赖原资料版本变化时知识页失效；当前内容不再参与检索，历史正文仅供重新整理和保留人工修改，不能当作当前事实填表。
- 人工编辑后 protected=true，Agent 不能覆盖；需另作关联补充并保留人工判断。
- 遗忘先提交不可召回标记，再异步清理底层正文。失败保留清理队列重试；到期记忆也停止召回。
- 原资料按批次同步，支持断点续传；每次最多十个文本分片，不持有整个大文件导出的长事务。
- 当前路由/MCP 不再提供旧候选采纳、旧个人档案、字段映射与表单生成业务。账户、原件、上传、初始化、容量和变更历史接口继续服务。

## 本机迁移与回滚

1. 已在 `work/tencent-cutover-20261025/` 保留原环境配置、PostgreSQL 自定义格式备份及前端切换回执；备份清单可读取。原切换阶段未演练；后续已完成独立 PostgreSQL 及腾讯本地数据恢复演练，外部 Cloudreve 文件卷未覆盖。
2. 增量迁移 0017—0020 添加租户隔离表和仅 Worker 可用的清理发现函数，不删除旧表。当前 `.env` 明确 `AKB_KNOWLEDGE_ENGINE=tencent`。
3. 原 137 份来源保留；本轮盲测另外增加六份。已迁移 160 条历史人工修改和非 Agent 复核记录。旧记录没有 actor 时标记 `historical_unspecified`，不冒充明确的用户确认。旧模型生成事实不自动重新认证。
4. 本地两个自有 WorkBuddy 专家的技能/代理说明已备份并同步新契约；MCP 配置、凭据与信任文件未被改写。宿主原安装版本标签保留；新生成接入包为 0.7.1。
5. 应用回退可以停止 API/Worker，显式切换 `AKB_KNOWLEDGE_ENGINE=legacy` 后重启。腾讯数据保留；**切换后新写入的腾讯内容不会自动反向迁回旧引擎**。因此回滚前仍需备份两侧数据。
6. 前端切换脚本保留上一个 statics 目录，回执在对应 `work/cloudreve-custom/<revision>/deployment.json`。恢复只切换静态目录并重启 Cloudreve，不删除文件卷。浏览器通过原生“更新”按钮刷新 Service Worker。

## 启动与验证

```bash
cd /Users/alex/workspace/agent-knowledge-base
.venv/bin/python scripts/setup_tencent_memory.py
# 只校验已有运行时，不修改正在运行的依赖
.venv/bin/python scripts/setup_tencent_memory.py --check
.venv/bin/alembic upgrade head
.venv/bin/python scripts/dev.py

# 真实腾讯服务必须先运行
AKB_TEST_TENCENT=1 .venv/bin/pytest backend/tests/test_tencent_memory.py
.venv/bin/python scripts/smoke_tencent_memory.py
```

准备脚本固定 macOS arm64 / Node 22.23.2，校验归档，不安装全局 Node 或 OpenClaw。其他平台安装尚未实现。启动器等待两项服务健康后启动应用；只活一项时停止，避免悄悄启动半可用服务。

实际验收结果另见 [本轮盲测](tencent-workbuddy-acceptance.md)。固定测试、MCP 跨进程验证、真实 WorkBuddy 任务分别计分。

## 仍存在的工程边界

- 原生短专名召回、长文质量、复杂表格、图片/文本框覆盖仍有限；没有 embedding 语义检索和大样本准确率证明。
- PostgreSQL 与腾讯 SQLite/Wiki 不是分布式事务。失败的未登记远端修订不会被读出，但新增有界清单核对和写入日志恢复，故障注入已通过；并非所有崩溃时点或无限规模已验收。
- 后端容量账本已包含腾讯逻辑内容，Cloudreve 原生侧边栏及磁盘实际开销仍未统一；多实例、跨主机、HA、远程 TLS MCP、正式插件发布与无人值守模型调度均未验收。
- 用模型重新整理不等于事实天然可靠。本版把统计、单位、身份、角色与时效的判断交给 WorkBuddy 并回原文核查；已补充通用算术、原文锚点和输出回读门禁；仍须用盲测衡量模型的完整性与语义判断。
