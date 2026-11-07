# WeKnora + WorkBuddy 外部整理模式（2026-10-30）

## 本次实现范围

知序保留文件管理、原件、解析证据、配额、租户权限和填写校验；资料正文的索引与知识页/偏好/任务记忆改为可选择的 WeKnora 存储。摘要、分类、知识目录整理、语义改写由教师当前 WorkBuddy 对话执行。OCR 使用服务器本地 RapidOCR + ONNX Runtime CPU，不调用云端 OCR，也不要求教师安装模型。

这是新引擎的本机工程验收版。旧 Tencent Memory 数据没有迁移，现有正式配置没有切换，校园部署和 WorkBuddy 真实模型新一轮盲测尚未完成。自动化测试中的知识页是测试输入，不是模型盲测答案。

## 数据流

1. 教师在文件管理页上传 Word、Excel、PPT、PDF、图片等受支持资料，沿用 2 GiB 单文件上限与个人空间配额。上传大小不等于可完整解析大小；原有页数、字符、图片像素和解析预算仍有效。
2. 后台提取原文；扫描 PDF、图片调用 CPU OCR。保留原件、页码/工作表/单元格/幻灯片定位、识别置信度和来源版本。OCR 结果不能等同人工确认。
3. 原文按版本同步到 WeKnora 的私有来源库，建立关键词索引。不经过大模型就可以查找原文。
4. 每份新来源版本生成整理待办。WorkBuddy 在教师当前授权会话中领取、阅读、生成带出处的主题页，原子完成回写。页面和目录是用户与 Agent 共用的数据。
5. 填表仍读取当前原文，使用既有 Decimal 计算、单位转换、字段契约和产物回读校验。整理页帮助定位，不替代事实证据。
6. 文件修改后旧任务被替代、依赖旧版本的页面失效，新版本生成待办；人工页及历史内容继续保留。

## 分工与费用

| 工作 | 执行位置 | 模型/资源 |
| --- | --- | --- |
| 原件保存、预览、权限、任务队列 | 校园服务端 | 磁盘、数据库、CPU |
| Office/PDF 提取、扫描件 OCR | 校园服务端 | RapidOCR/ONNX Runtime CPU |
| 原文索引与关键词检索 | 私有 WeKnora | SQLite FTS，本次未启用向量 |
| 摘要、分类、目录、写作与填表推理 | 教师 WorkBuddy | 教师当前账号的模型用量 |
| 金额、统计、证据版本与字段校验 | 知序服务端 | 确定性程序，无模型 API |

此模式不把 WorkBuddy 变成服务器随时可以调用的模型 API。教师退出、设备离线或宿主未运行任务时，语义工作等待；服务器的文件解析和原文索引仍可继续。没有承诺全天候无人值守整理，也没有自动购买模型额度。

未启用 Embedding / Rerank API。关键词检索不等于语义检索，近义表达需要 WorkBuddy 改写查询并核对原文。校园规模的并发和召回率还需另外验收。

## WeKnora 变更与维护边界

上游：[Tencent/WeKnora](https://github.com/Tencent/WeKnora)，固定提交 `40192ace214ca046a86b590d69c1fe7286f3423e`。压缩包校验值及模式在 `integrations/weknora/upstream.json`，修改集中为 `external-executor.patch`。

原版即使只开关键词索引也仍有获取 Embedding 模型的路径，不能仅关掉向量开关就宣布不需要模型。本补丁提供显式 `WEKNORA_EXTERNAL_EXECUTOR=workbuddy` 模式：

- 关键词批量入库无需 Embedding；所有聊天、向量、重排、视觉、语音模型入口拒绝服务端推理。
- 禁止该模式的 Markdown 图片自动解析/远程抓取，关闭摘要、问答生成、自动标签、图谱和资料画像提炼。
- 原文以不可变 JSON 封套保存，索引时单独解包，防止转义换行污染英文分词。
- 原生列表增加轻量盘点输出，配额检查不再逐条下载全文。
- Apple Silicon 构建依赖 go-m1cpu 更新为 v0.2.2，修复当前机器启动时上游旧依赖崩溃。

每位教师拥有独立原生账号和两份知识库；服务凭据仅授权这两份库的读取/入库能力，并保存在服务器私有文件，不能交给模型。WeKnora 仅监听回环地址，教师通过知序 OAuth MCP 访问。引擎不匹配或不可用时明确失败，没有静默切回旧引擎。

## WorkBuddy 接入

教师流程继续是：打开 WorkBuddy → 安装/打开知序教师助理 → 登录并授权自己的文件空间 → 在同一对话回答称呼、身份和用途 → 上传或指定资料 → 助理完成整理并试用。相关问题可以合并回答，安装后不要求再去网页点击初始化。

初版接入包版本为 0.10.0，增加五个工具：`list_organization_jobs`、`claim_organization_job`、`renew_organization_job`、`complete_organization_job`、`release_organization_job`。租约默认 15 分钟，带版本/凭据校验；重复完成按请求摘要回放；重领后旧 token 失效。临时中断可重试；超过 8 次或需要用户判断时保留 blocked 状态，不冒充完成。当前 blocked 的同版本任务尚无用户界面重试入口；重解析/新版本会生成新任务。

0.11.0 起，本机索引脚本和纯 Python PDF 读取器随 WorkBuddy Skill 分发，使用宿主运行环境；教师无需另装客户端、Python 或 OCR。远程 MCP 签发单文件/单设备清单的短期上传许可，仍不能读取校园服务器目录冒充教师电脑。详见 [助理内本机检索](workbuddy-host-documents-20261030.md)。

`outputs/weknora-assistant-20261030/preview-final/` 是不含个人凭据的结构验收包，使用保留域名 documents.example.test，不能直接分发给教师，也没有发布到 WorkBuddy 市场。部署时以真实校内可达的 HTTPS 域名和作者邮箱重新构建。

## 管理员构建与启动

以下仅在服务端执行，不交给教师。需要 Python 3.12 虚拟环境、Go 1.26.3、C 编译工具链；首次准备依赖和 OCR 权重需要下载，实际 OCR 推理在 CPU 本机执行。

```sh
.venv/bin/python scripts/setup_weknora.py --directory work/weknora-runtime
.venv/bin/python scripts/run_weknora.py \
  --binary work/weknora-runtime/weknora \
  --source work/weknora-runtime/WeKnora-40192ace214ca046a86b590d69c1fe7286f3423e \
  --state data/weknora-runtime
```

构建器先检查固定源码 SHA-256，再检查和应用补丁；拒绝覆盖已有源码目录。运行器只继承必要环境，密钥保存在权限 0600 的私有文件，不继承模型 API 密钥。用进程守护管理启动脚本；本次测试使用终端进程，尚未安装校园开机自启。

新测试环境配置：

```dotenv
AKB_KNOWLEDGE_ENGINE=weknora
AKB_WEKNORA_URL=http://127.0.0.1:5881
AKB_WEKNORA_SECRETS_DIR=./secrets/weknora
AKB_AGENT_TASK_LEASE_SECONDS=900
```

先备份选定数据库和私有状态，再在该环境执行 `.venv/bin/alembic upgrade head`（0023）。随后启动 API 与 worker；`scripts/dev.py` 会先核对 WeKnora 模式，后端未就绪时不启动 API。不要将真实旧库的引擎变量改成 weknora 后直接运行。

远程助理包：

```sh
.venv/bin/python scripts/build_teacher_assistant.py \
  --engine weknora \
  --origin https://实际部署域名 \
  --author-email 实际作者邮箱 \
  --output outputs/teacher-assistant-release
```

## 升级、迁移和回滚

0023 是新增引擎绑定与持久化任务表，不自动搬迁正文、不改原件。既有绑定明确标记 tencent，新引擎访问会返回冲突，防止误认旧库已迁移。

正式切换仍须：冻结写入并备份 → 从旧引擎导出知识页/偏好/任务正文及版本 → 在独立目标环境重新导入原件和派生内容 → 比较内容摘要、引用、人工修改、配额与租户隔离 → 真实 WorkBuddy 验收 → 切换入口。该迁移流程尚未实现为一键工具。

不要回滚执行 DROP 表或直接改绑定字段。旧版本代码的 schema 检查可能拒绝 0023，需使用兼容代码，或恢复整套已验证备份及相应配置；不能只回滚数据库而继续写新引擎。旧数据当前保持原样。

## 验收与已知限制

本轮工程验收结果见同目录输出 `outputs/weknora-assistant-20261030/acceptance-report.json`。本机使用 WeKnora Lite SQLite/FTS、独立 PostgreSQL 测试库和合成教师资料；没有测试校园生产数据库、多人规模负载或故障切换。

已验证 Word/Excel/PPT/扫描 PDF/图片的解析、无模型原文检索、目录回写、人工修改保护、来源增量失效、金额转换、租约重试与跨租户隔离。中文扫描样张识别到姓名、批准经费、到账金额和未结题状态，三个 ONNX 会话均为 CPUExecutionProvider。这是样张正确，不是通用 OCR 准确率或手写识别承诺。

引文校验能证实原文片段和当前版本，不能证明所有语义改写都正确，也不能发现 Agent 漏读了某份材料。自由写作、统计完备性、复杂合并表头、低清扫描仍需真实模型与真实任务验收。目录回写的技术测试不等于 WorkBuddy 已真实完成新一轮盲测。
