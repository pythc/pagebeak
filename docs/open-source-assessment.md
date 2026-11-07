# 开源选型与决策记录

版本：v0.1

核查日期：2026-10-16

2026-10-25 追加研究：[Agent 记忆项目与组合方案](agent-memory-research-20261025.md)、[TencentDB Agent Memory 专项调研](tencent-agent-memory-research-20261025.md)。补充腾讯项目后，将 TencentDB 与 Hindsight 并列为一级候选：优先验证目录增量及人工保护，再对照跨会话记忆；Mem0 为轻量备选。若不提供后台模型服务，评估宿主 Agent 提炼路线。此为新设计建议，尚未部署，不改写下方 v0.1 的历史选型状态。

范围：官方仓库、许可证、发行记录和文档；未进行部署、源码安全审计或性能验证

## 1. 评估依据

产品需求为多用户独立知识库，核心链路包含文件处理、自动整理、个人档案、知识提取、网络补全和表单填写。选择依据是能力边界、可追溯性、二次开发成本、多用户隔离、维护成本和许可证；不以 Star 数或项目自行公布的 benchmark 直接推断本场景质量。

表中的“推荐”均是本产品的设计判断。“官方已有”只表示文档或仓库声明存在，不表示本项目已经验收。

## 2. 候选项目

| 项目 | 官方已声明的相关能力 | 许可核查 | 本产品处理方式 |
| --- | --- | --- | --- |
| [RAGFlow](https://github.com/infiniflow/ragflow) | 多格式文档处理、带引用检索、Agent、知识编译 | 主项目 Apache-2.0 | 优先验证为内部文档与检索服务 |
| [OpenViking](https://github.com/volcengine/OpenViking) | 统一资料和记忆的虚拟文件系统、摘要/概览/正文分层、用户记忆、账户隔离 | 当前主项目 AGPLv3，CLI/示例等有不同许可 | 做目录与记忆能力的对照方案；采用前核对固定版本及集成方式 |
| [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm) | 文档对话、Agent、MCP、记忆、定时任务；Docker 版支持多用户 | MIT | 适合快速体验原型，不选为本版事实档案核心 |
| [Onyx](https://github.com/onyx-dot-app/onyx) | 多应用连接、检索、Agent、外部动作 | 社区版 MIT；企业版另有功能与许可边界 | 组织知识整合需求变强时重新评估 |
| [Memobase](https://github.com/memodb-io/memobase) | 用户画像、事件时间线、自定义画像配置 | Apache-2.0 | 参考画像建模；不额外部署为精确个人事实的唯一存储 |
| [Mem0](https://github.com/mem0ai/mem0) | Agent 的持续记忆基础设施 | Apache-2.0 | 对话偏好等记忆需求明确后再评估，避免与档案重复存储 |
| [memU](https://github.com/NevaMind-AI/memU) | 当前 README 侧重跨会话/Agent 的个人 Wiki 记忆 | Apache-2.0 | 参考记忆整理模式，不以旧版宣传推断当前完整产品能力 |
| [Cognee](https://github.com/topoteretes/cognee) | 将资料和对话整理为可检索知识图谱，支持领域模型 | Apache-2.0 | 关系推理需求达到必要程度后再引入 |
| [Open Notebook](https://github.com/lfnovo/open-notebook) | 多模态研究资料组织、全文与向量检索、引用问答 | MIT | 参考资料阅读和专题组织体验 |
| [Dify](https://github.com/langgenius/dify) | Agent 工作流、RAG 和应用编排 | Apache-2.0 基础加附加条件 | 需先解决计划部署方式是否触发额外授权，不作为默认底座 |
| [FastGPT](https://github.com/labring/FastGPT) | 知识库与可视化应用工作流 | Apache-2.0 基础加附加条件 | 与 Dify 一样先核对额外条款，不直接按宽松许可规划多租户产品 |

## 3. 与选择直接有关的事实

### 3.1 RAGFlow 的整理功能与版本

官方知识编译文档描述了目录树、图谱、页面索引、思维导图、时间线等派生内容。它能减少通用整理功能的开发量；但个人主体归属、时效、用户修正优先级和表单字段规则仍属于本产品业务。[官方知识编译文档](https://ragflow.io/docs/v1.0.0-rc2/knowledge_compilation/overview)

截至核查时，最新发布为 `v1.0.0-rc2`，发布说明明确是 1.0 的第二个候选版本。`v0.27.2` 的发布说明已包含知识编译相关配置与改进。不能把候选版当作已验证的生产稳定版本，也不能把旧版自动视为安全或更合适。[rc2 发布说明](https://github.com/infiniflow/ragflow/releases/tag/v1.0.0-rc2)、[v0.27.2 发布说明](https://github.com/infiniflow/ragflow/releases/tag/v0.27.2)

PoC 建议先固定 `v1.0.0-rc2` 验证适配契约；出现关键阻塞时以 `v0.27.2` 作版本对照，单独记录功能和数据兼容差异。生产版本保持 `TBD`，不能在文档阶段宣称升级或回滚已验证。

公开 HTTP API 是适配优先入口。需要实际验证的内容包括证据定位、解析结构导出、召回前权限范围、身份创建、后台任务和删除对账，不因文档出现一个 API 就推断整条业务链路成立。[HTTP API](https://ragflow.io/docs/http_api_reference)

### 3.2 OpenViking 的优势与边界

OpenViking 的分层上下文和目录浏览方式很接近“Agent 主动整理、按需读取个人知识”的目标。官方还区分账户内共享资料、用户私有资料和记忆，不能把任意目录都当作默认私有。[多租户与隔离文档](https://docs.openviking.ai/en/concepts/11-multi-tenant)

当前 README 将主项目标注为 AGPLv3，并分别说明 CLI、示例和适配器的许可；同时有额外说明。采用时需要依据固定版本的 LICENSE、README 及实际改动和部署方式核对义务。AGPL 不等于禁止商业使用，也不能只因将服务单独部署就断言不存在相应义务。[当前许可说明](https://github.com/volcengine/OpenViking#license)、[LICENSE](https://github.com/volcengine/OpenViking/blob/main/LICENSE)

未选择它作为第一版默认底座的原因是：本版优先验证复杂文件与精确填表，个人事实和来源机制仍需要自研；同时部署两套上下文核心会增加数据同步与权限维护成本。这是范围和成本判断，不是对其准确率或成熟度的实测结论。

### 3.3 聊天记忆不能直接等同于个人档案

AnythingLLM 官方文档中的记忆主要用于给对话提供用户背景，区分全局和工作区记忆，并限制每次注入内容。它有实际用户记忆能力，但不能据此推断其原生满足全部论文列表、精确统计、完整证据链和历史任职等需求。[记忆文档](https://docs.anythingllm.com/features/memories)

Memobase 提供画像和时间事件的可配置模型，适合作为设计参考。对本产品而言，证据、用户更正和确定性查询的业务模型仍由自己的数据库持有。[Memobase 官方仓库](https://github.com/memodb-io/memobase)

### 3.4 Dify / FastGPT 的额外许可条件

Dify 的许可证对多租户环境和前端标识列有额外条件，并把 workspace 与 tenant 联系起来。具体部署是否触发条件需要按其原文核对，不能笼统声称所有后端调用都需要额外商业许可。[Dify LICENSE](https://github.com/langgenius/dify/blob/main/LICENSE)

FastGPT 的额外条件包括运营与其类似的多租户 SaaS 和控制台标识。其文字边界与 Dify 不完全相同，不应混用解释。[FastGPT LICENSE](https://github.com/labring/FastGPT/blob/main/LICENSE)

本项目目前未确定商业模式与是否修改上游前端；在这些条件未明确前，优先验证许可更直接的组合，避免先形成深度依赖再处理授权。

## 4. 决策记录

以下状态为“设计建议”，不是用户已逐项批准或实际测试通过。

| 决策 | 建议 | 理由 | 重新评估条件 |
| --- | --- | --- | --- |
| D01 业务与引擎分离 | 业务数据、权限和事实归属由自研服务持有 | 产品核心规则不能依赖聊天提示词；保留迁移能力 | 如现有服务有明确等价契约并通过完整验证 |
| D02 文档底座 | 首先验证 RAGFlow | 复用多格式处理、检索、引用和整理功能 | 定位、隔离、删除、稳定性或成本不满足门槛 |
| D03 档案存储 | PostgreSQL 保存声明和投影 | 精确字段、时间、关系及统计可确定性处理 | 有明显且可测量的复杂图查询瓶颈 |
| D04 目录 | 虚拟目录与派生页面 | 不移动原件；支持多主题归类和可重建 | 后续用户需要同步物理文件目录 |
| D05 自动执行 | 受规则约束的事件任务与周期任务 | 有预算、可停止、可追踪，不无限循环 | 更复杂的长期规划确有业务收益 |
| D06 表单 | 先结构化准备，再接有限外部站点 | 字段正确性与网页执行成功率分开验证 | 自有表单闭环通过后 |
| D07 图谱与额外记忆库 | v0.1 不单独部署 | 当前需求可由结构化关系和文档检索覆盖 | 跨领域关系推理成为核心需求 |

## 5. 辅助组件与替代路线

- **Docling**：MIT 项目，提供多格式解析与统一文档表示；如果 RAGFlow 的公开产物无法满足证据定位，评估它作为解析组件。模型权重和相关依赖另行核对，不把代码许可证等同于所有依赖许可。[官方仓库](https://github.com/docling-project/docling)
- **LangGraph**：用于有状态、可恢复的 Agent 步骤。必须配置持久化，副作用仍需要业务幂等；它不能自动替代任务调度、身份和授权。[持久化文档](https://docs.langchain.com/oss/python/langgraph/persistence)
- **Browser Use**：开源库可作为后续浏览器执行候选；其托管服务与模型调用是单独的服务和费用，不能混为“开源即全部免费”。[官方仓库](https://github.com/browser-use/browser-use)
- **网络抓取**：第一版优先直接 API 和受限网页导入；复杂网站再评估抓取库，避免首期引入与引擎重叠的整套爬虫平台。

替代路线为 Docling + PostgreSQL/pgvector + 自研检索和编排。代价是自己负责分块、关键词检索、语义融合、重排、引用预览和索引维护；不能将部署组件变少直接等同于总成本更低。

## 6. 采用前核对清单

固定依赖 tag、commit 与镜像 digest；记录 LICENSE/NOTICE、模型权重和依赖许可；检查部署平台支持、已知安全问题和升级说明；验证所有必需能力确实存在于使用的开源版本中。最终采用记录应链接实际 PoC 结果，不能只链接 README。

本轮结果仅构成调研与方案依据。未核验源码级隔离实现，未复现实验指标，也未对任一候选作生产安全保证。
