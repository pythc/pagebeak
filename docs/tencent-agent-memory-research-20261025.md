> 2026-10-25：当前实现已切换为腾讯 Memory + WorkBuddy 外部 Agent 模式。本文相关旧设计为历史记录，以 [当前切换方案](tencent-memory-cutover.md) 为准。

# TencentDB Agent Memory 调研与知序适配建议

日期：2026-10-25。状态：官方资料及定点源码核查，运行验证 `NOT_RUN`。

## 1. 结论与选型调整

本次重点是 [TencentCloud/TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)。腾讯混元的 [Hy-Memory](https://memory.hunyuan.tencent.com/) 是另一条模型、框架、MemHub 应用路线；腾讯云托管 Agent Memory 也不能与开源自建版的存储、部署和服务承诺混为一谈。

上一轮漏掉 TencentDB Agent Memory，候选范围不完整。补充核查后，建议将其与 Hindsight 并列为一级候选：**优先用 TencentDB 验证目录生成、资料增量和人工修改保护，以 Hindsight 对照跨会话记忆及召回。最终只选一个记忆主引擎。** 此顺序来自需求匹配，不是准确率或稳定性实测排名。

推荐验证的组合：**Cloudreve + 知序现有文件解析/事实/证据服务 + TencentDB MemoryCore + 按需接入 MemoryKnowledge Wiki + 知序统一 MCP。**

Cloudreve 继续承载用户文件页面。精确填表和统计继续使用知序事实库。先不引入 CodeGraph；Memory Hub 可用于内部调试，不替换现有用户界面。Proxy 作为后续可选接入方式，不能在调研阶段改动用户当前模型配置。

## 2. 版本与核查依据

Git 远端引用实际读取结果：

| 对象 | commit |
| --- | --- |
| 当前默认分支 `feat/server_team` | `6a4bb55ea54834d8265ce52024d3e3c75db30d38` |
| `v2.0.2` | `99a1a2c251741658917e3fa700bf8e337ad15a5f` |
| `v2.0.3-beta.1` | `badbd2179d06f4e0e1f2133bd3482880beab8a02` |

发布页列出 v2.0.2（2026-10-09）和 v2.0.3-beta.1（2026-10-13），后者被页面标为 Latest，但名称仍带 beta。建议将 v2.0.2 作为初始版本候选，再与固定默认分支做差异核对；本次默认分支源码结论不能直接当成 v2.0.2 已有能力。[发布记录](https://github.com/TencentCloud/TencentDB-Agent-Memory/releases)

本次读取了官方安装、WorkBuddy、MemoryCore、MemoryKnowledge 文档，以及固定 commit 的路由、增量分类、Wiki 合并和鉴权实现。没有成功取得完整本地 checkout；没有运行安装脚本或上游代码。下文“源码存在”不代表测试通过。

主项目许可证为 MIT，具体模块、复用项目及模型依赖仍需随固定版本核对。[LICENSE](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/6a4bb55ea54834d8265ce52024d3e3c75db30d38/LICENSE)

## 3. 核心能力与模块边界

当前 2.x 产品将记忆组织为 Chat Memory、Skill、Wiki、CodeGraph 等资产。团队治理包含所有者、版本、可见性和 Agent 绑定。[项目说明](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/README_CN.md)

| 模块 | 上游提供什么 | 知序如何使用 |
| --- | --- | --- |
| MemoryCore | 对话及分层记忆、资产元数据、API/SDK | 保存跨会话偏好、经验和任务上下文 |
| MemoryKnowledge | Wiki 页面生成、检索、关系图；代码图谱 | 优先验证 Wiki，保留当前解析器与精确证据 |
| MemoryPanel / Hub | 团队、Agent 和资产的操作界面 | 内部调试；用户继续在 Cloudreve 查看知识目录 |
| MemoryProxy | 在模型请求中装配记忆、转发并收集会话 | 可选；首次通过知序 MCP 对接 Core，控制集成范围 |

MemoryCore 不运行或调度 Agent。其 standalone 默认 SQLite、本地文件和进程内状态，可不配远程 embedding，依靠 BM25；提炼与聚合仍需 LLM API。它是可自建服务，不要求把资料放到腾讯云向量数据库，但不能由“本地存储”推导“模型推理也全部本地”。[MemoryCore](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryCore/README.md)

LLM-Wiki 提供文档到页面和链接关系的整理；Wiki 服务与 Panel 存在模型配置及状态回调协作。直接拆出 Wiki 需要适配这些契约，并非导入一个 npm 包即可替换现有后台。[MemoryKnowledge](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryKnowledge/README.md)

## 4. 对我们最有价值的实现

### 4.1 分层记忆与人可读目录

其长期记忆从 L0 原始对话、L1 原子信息、L2 场景到 L3 长期画像逐级组织。这个层级可以映射到知序的“原文—证据—专题—个人概览”，但不直接复用为事实权威来源。上层概括必须能回到我们的 source/evidence ID。[分层设计](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/README_CN.md)

Wiki 产物是 Markdown 页面及索引，有来源列表和页面链接。我们可将这些内容投影到现有目录；用户与 Agent 读取同一个节点、版本和来源集合，无需使用上游的 Team/Agent/Task 页面。

### 4.2 已有基于哈希的增量判断

固定 commit 的 `classifySources` 比较源文件 SHA-256 和上次抽取状态：新增、内容变化、此前失败的源进入处理；成功且未变的源跳过；缺失源进入删除分类。`runIngestIncremental` 实际调用该分类，并分别安排抽取与合并。[增量分类源码](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/6a4bb55ea54834d8265ce52024d3e3c75db30d38/MemoryKnowledge/src/engines/wiki/index-db.ts#L284)、[调用链](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/6a4bb55ea54834d8265ce52024d3e3c75db30d38/MemoryKnowledge/src/engines/wiki/manager.ts#L521)

这是我们此前提出的哈希变更机制的可复用基础，但它检测的是 Wiki 输入源；Cloudreve 文件动作、上传完成、解析失败重试、重命名和删除历史仍由我们的事件桥接。写入 Wiki raw 后还需要触发 ingest，不能假定所有上传会自动启动全链路。

### 4.3 人工修改保护有实际代码

`pageWrite` 为编辑后的页面添加 `locked: true`；`mergePage` 遇到锁定页面直接跳过合并。[写入锁标记](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/6a4bb55ea54834d8265ce52024d3e3c75db30d38/MemoryKnowledge/src/store/wiki-service.ts#L893)、[合并保护](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/6a4bb55ea54834d8265ce52024d3e3c75db30d38/MemoryKnowledge/src/engines/wiki/ingest-v2/merge.ts#L65)

这与保留人工修改的需求相符，但保护粒度是整页。用户改了一句话后，合理新增内容也可能不再进入该页。建议继续保留知序的自动内容/人工覆盖分层；新资料涉及锁页时显示待合并差异，不静默忽略。

## 5. WorkBuddy 接入：真实条件

官方 WorkBuddy 文档通过 `~/.workbuddy/models.json` 注册自定义模型，将模型地址指向 MemoryProxy，再在 WorkBuddy 中选择该模型。请求经 Proxy 转发至配置的模型上游。另有历史 Skill/session 导入工具。[WorkBuddy 配置](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/agents/workbuddy/README.md)、[历史导入](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/agents/workbuddy/asset-import.md)

因此：

- 官方 Proxy 方案不只是增加一个 MCP；它改变选定模型的调用路径。
- 需要可调用的上游模型 API。不能承诺 WorkBuddy 内置模型额度可直接用于该 Proxy。
- 可替代地用知序现有 MCP 调用 MemoryCore HTTP API；这是我们拟实现的适配，不是已经存在的官方 WorkBuddy MCP 一键包。
- 仅暴露 MCP 工具不会自动捕获所有对话；先由 Skill 在任务开始/结束时召回和保存选定内容，再验证客户端触发机制。
- 历史导入应预览并选择范围，不运行全量导入参数。当前项目调研没有授权采集真实聊天历史。

整套部署将记忆内部模型和 Proxy 上游模型分为两组配置，两者可以使用相同服务，也可以分别选型。这意味着自建的成本应分别计算 Agent 主模型和后台提炼模型，而非只算存储。[部署参数](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/deploy/global-images/.env.example)

## 6. 需要我们补齐或验证的部分

### 6.1 不能替换现有大文件及 Office 解析入口

固定 commit 的 `/wiki/raw/write` 接收 `{filename, content}` 文本，限制单项 512 KiB、一次最多 10 项、合计 5 MiB。这个接口不是 2 GB 原始 Office/PDF 上传接口。[路由源码](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/6a4bb55ea54834d8265ce52024d3e3c75db30d38/MemoryKnowledge/src/routes/wiki.ts#L248)

应继续使用知序上传、OCR 和格式解析，再导出带稳定 source ID、revision、页码/表格定位的 Markdown 分片。以上大小是此接口的代码限制，不声称整个腾讯产品所有入口均受同一限制。

### 6.2 多用户产品需要自己的外层权限边界

Core 数据接口与元数据接口的鉴权层次不同；文档说明部分删除操作信任服务端 Bearer 和实例标识。Knowledge 的源码则在未配置 service key 时放行；即便配置了 key，部分读取端点仍在白名单内。这是内部服务信任设计，不应直接视为面向互联网的用户授权体系。[Core API](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/MemoryCore/v3-api-memorycore-doc.md)、[Knowledge 鉴权源码](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/6a4bb55ea54834d8265ce52024d3e3c75db30d38/MemoryKnowledge/src/middleware/auth.ts)

建议客户端只访问知序网关；网关从登录身份/绑定令牌确定 service、team、user、agent，拒绝客户端任意指定隔离字段。底层服务仅内部可达。个人账户不能仅映射成公共团队的一个可任意选取的 ID；跨层 profile scope、缓存及队列同样要验证隔离。本轮不是完整安全审计，没有运行越权测试。

### 6.3 “有删除接口”不等于完整遗忘

Wiki 有来源级联清理路径；Core 也有分层删除接口。但定点读取的 L0/L1 删除 handler 不足以证明所有 L2/L3、Wiki、缓存、备份与未完成任务会同步清除。不能据此宣称已经实现不复活的跨层遗忘。[Core 删除实现](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/6a4bb55ea54834d8265ce52024d3e3c75db30d38/MemoryCore/src/gateway/v2-router.ts#L935)

知序仍需维护来源依赖、失效标记和幂等事件；删除后在重启、重放、再次提炼等条件下进行验收。锁定页保留人工内容与删除原始来源之间的冲突也要单独处理。

### 6.4 事实、数值和画像的可靠性不能由层级保证

L3 Persona 是模型整理结果，不能代替已采纳事实。填表继续走现有主体归属、引用校验、日期、单位和统计规则。Agent 仍可按已有授权自行采纳，人工更正继续优先。

源码中小页合并由 LLM 重写，大页采用追加。应验收冲突、重复统计、历史信息残留，以及异常输出后的恢复；不能仅因保留 sources 就认定每一句都被原文支持。

### 6.5 开源部署的扩展与运行条件

SQLite 可以支持单节点方案，但不能仅凭云托管版能力承诺开源版同等高可用和多实例写入。安装文档将 MongoDB 后端标为实验特性，切换后端不会自动迁移已有数据。先做单节点隔离 PoC，再按容量与并发测量确定部署方案。[安装说明](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/feat/server_team/INSTALL_CN.md)

检索还需检查 FTS5 是否可用。Wiki 索引源码包含中文 bigram 预分词，说明已考虑中文，但效果仍需在我们的人名、职称、论文与表格语料上测量；不能由中文文档推断中文召回一定优于 Hindsight。[Wiki 索引](https://github.com/TencentCloud/TencentDB-Agent-Memory/blob/6a4bb55ea54834d8265ce52024d3e3c75db30d38/MemoryKnowledge/src/engines/wiki/index-db.ts#L65)

## 7. 与 Hindsight 的实际选择

Hindsight 的依据见 [上一轮调研](agent-memory-research-20261025.md)，下表为需求适配判断，不是同机性能结果。

| 维度 | TencentDB Agent Memory | Hindsight | 本项目判断 |
| --- | --- | --- | --- |
| 目录与增量 | 已核查 Wiki、哈希分类与锁页实现 | 有持续整理知识页面 | 先验证 TencentDB 对目录需求的覆盖 |
| 接入形态 | Core API；官方 WorkBuddy 主路线走模型 Proxy | HTTP/MCP；有维护者 WorkBuddy hooks 适配 | 首轮统一通过知序 MCP，降低比较中的变量 |
| 数据底座 | 默认 SQLite/本地文件；Mongo 可选实验 | PostgreSQL 部署路线 | Hindsight 与现有数据库运维较接近；腾讯路线需新增运行时/存储维护 |
| 产品范围 | 团队资产、Skill、Wiki、CodeGraph、控制台 | 记忆提炼、召回和整理 | 腾讯覆盖更广，也更容易与现有产品重复 |
| 精确填表 | 仍需我们的事实与验证服务 | 同样需要 | 都不能取代现有业务核心 |
| 中文/成本/隔离 | 尚未实测 | 尚未实测 | 用同一语料与尽可能一致的模型条件比较 |

若目录和增量闭环达到要求，可用 TencentDB 替换原方案中的 Hindsight 候选位置；不把两者叠成双写记忆库。若拆分 Wiki/Panel 成本过高或运维门槛不合适，则保留 Hindsight 路线并借鉴腾讯的增量与锁页机制。

## 8. 建议的最小验证

第一组为全新的中文办公资料：简历、聘任书、项目记录、论文、表格与扫描证明。先由现有解析器处理，再输入 Wiki，验收目录、主题聚合、来源导航和错误归属。

第二组为变化：替换文件、改名、删除、重复事件、人工编辑页面后再上传新资料。记录哪些源重新提炼、模型调用量、旧内容是否失效，特别验证锁页是否导致新增资料静默遗漏。

第三组为跨会话：偏好更正、同名人物、计划与已完成、任务到期、两个租户的隔离和明确遗忘。通过原有网页/Word 填表回归验证新增记忆不破坏准确性。

第四组为运行失败：LLM 超时/格式异常、后台重启、数据卷恢复、任务重复投递和版本回滚。固定 commit、模型、提示词与镜像 digest，不使用漂移的 latest 镜像验收。

硬门槛建议沿用：跨租户泄露、遗忘后复活、人工更正被覆盖均不得在测试集中出现；原有填表正确性不得退步。其他指标分别记录召回、目录质量、错误记忆、上下文体积、延迟和模型费用，不用官方单一 benchmark 替代本产品验收。

本轮交付为调研与设计修订；未部署、未改 WorkBuddy 配置、未执行模型测试。
