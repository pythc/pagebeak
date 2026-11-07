# 大文件、OCR 与个人空间

## 需求与实现

用户要求：常见办公材料统一入库、解析、索引、摘要与目录；单文件上限改为 2g；选择轻量 OCR；管理员为用户分配容量；Agent 获取容量并提出建议，在用户批准或授权后清理。

2g 定义为 **2 GiB = 2,147,483,648 字节**。每人默认 10 GiB，可由管理员调整；这是可配置初始值，不是用户指定的套餐。个人逻辑配额与服务器真实磁盘余量分别检查。

## 统一入库

先按完整文件长度预留配额，再以 8 MiB 分块上传。校验顺序和长度；应答丢失时查询 offset。暂存与原件在同一文件系统，以硬链接发布，不把 2 GiB 文件整体读入内存。下载使用流式 FileResponse，支持 Range。

接口支持查询 offset、续传、取消及幂等完成。网页恢复当前操作中已成功提交的分块；刷新页面后自动恢复上传列表尚未实现。会话 24 小时过期，Worker 每分钟清理暂存；主动取消立即释放预留。

Worker 通过路径启动无服务凭据的解析子进程，保存可定位文本块。对所有已解析块生成跨全文的原文抽样摘要与关键词，最多 8 段，每段附 evidence_id 和位置。这是有损摘录，不是后台模型语义总结；WorkBuddy 可读取证据生成详细专题。

目录继续使用稳定 ID，网页和 Agent 共读；增量更新保留人工标题、分类、摘要、备注。算法升级为 v2，迁移 0006 为已开启自动整理的存量空间排入刷新；暂停者保持暂停。

| 格式 | 当前解析 | 限制 |
| --- | --- | --- |
| DOCX | 段落、基础表格及位置 | 图片、文本框和复杂布局不完整 |
| XLSX | 工作表、单元格、公式表达式及范围 | 不计算公式、不解析图表；单表 2 万行、200 列 |
| PPTX | 按页提取文本 | 图片、复杂表格和备注不完整 |
| PDF | 原生文字；无文本页尝试 OCR | 最多 300 页，其中每份最多 OCR 10 页；加密文件不支持 |
| PNG/JPG/JPEG/WebP/BMP/TIF/TIFF | OCR，保留行、矩形坐标和置信度 | 仅第一帧，最多 4000 万像素 |
| TXT/MD/HTML/HTM/CSV | UTF-8 文本，CSV 逐行结构 | 大文本读取前 8 MiB，标记部分解析 |
| DOC/XLS/PPT 等其他格式 | 保留原件 | 未接入转换，明确显示不支持解析 |

Office 展开限制 100 MiB/1 万 ZIP 条目；总解析最多 5000 块/200 万字符；子进程 100 秒超时、90 秒 CPU 限制。这不是操作系统级沙箱，也不承诺任意 2 GiB 文件完整解析。手写、复杂表格、公式与图片语义不属于本次完整能力。

## OCR 选择

采用 RapidOCR 3.10.0 + ONNX Runtime CPU。当前安装包随附 PP-OCRv6 small 检测/识别和方向分类模型，模型共约 30 MiB，无需 GPU 服务；未上传材料到外部 OCR。选择依据是部署规模、CPU 推理、中英文印刷体覆盖及坐标/置信度输出，不能据此宣称全场景准确率。

官方来源：[项目与许可证](https://github.com/RapidAI/RapidOCR)、[安装](https://rapidai.github.io/RapidOCRDocs/main/en/install_usage/rapidocr/install/)、[输出结构](https://rapidai.github.io/RapidOCRDocs/main/quickstart/)。未使用逐渐退休的 rapidocr_onnxruntime 包。

实际测试是清晰的合成中英文图片与扫描 PDF；真实低质量扫描、手写及复杂表格仍需独立评估。OCR 提取的姓名、邮箱也只产生候选，不自动确认为本人事实。

## 配额与管理后台

- 配额按**原件字节**统计，包括失败、不支持格式和等待物理删除的文件。available = max(0, quota - used - reserved)。解析文本、索引、页面与摘要等数据库内容暂不计入个人配额，仍消耗服务器资源；这不是物理磁盘总用量。
- 预留、完成、删除释放、配额调整使用租户行锁，防止并发超配。新配额不能小于已用与预留之和，管理员更新校验 expected_quota_bytes，防止覆盖别人修改。
- 使用量加预留达到 80% 告警、95% 紧张。GET /v1/storage 和 MCP 提供 quota/used/reserved/available/pending_deletion 字节数，网页与 Agent 共用。
- 网页“管理后台”及其 API 都检查管理员角色和浏览器会话；管理员 PAT 也不能调用。管理员能查看用户与容量，不能因此通过资料接口读取他人的文件。
- 注册默认非管理员，不按首个注册者提升。运维使用迁移数据库身份运行 `uv run python scripts/manage_admin.py --email 已注册邮箱`，`--revoke` 撤销。当前只为隔离合成验收账号初始化管理员角色，未提升真实账号。
- AKB_DEFAULT_QUOTA_BYTES 控制新账号默认容量；老账号由后台分配。AKB_DISK_RESERVE_BYTES 默认 256 MiB。配额不等于物理磁盘预留；跨用户并发可能遇到磁盘不足，写入返回 507 且不推进 offset。

## Agent 清理

新增 6 个工具，MCP 合计 18 个：get_storage_usage、list_cleanup_candidates、propose_storage_cleanup、read_storage_cleanup、execute_storage_cleanup、read_storage_archives。

流程：查看容量 → 判断用途与候选 → 提出具体清单 → 用户网页审阅/批准 → 用户或 Agent 执行 → 后台删除原件 → 回读剩余容量。

- 候选按大小最多列 50 份，一批最多 20 份；不自动判定重复或无用。
- delete 删除原件；summarize 先保留跨全文原文抽样及人工目录修改，再删除原件。这是有损归档，不能还原原件或保证保留所有条款、数字和附件。
- Agent 也可在读取原文后传入 summaries：每份归纳包含 source_id、content、citations（evidence_id 与逐字 quote）。服务核验每个引用来自该文件当前证据，保存引用位置及快照；不宣称已自动证明归纳稿全部语义正确。用户网页审阅后，该稿与人工目录内容一起归档，删除原件也不会丢失归纳稿。省略 summaries 时仅保留后台抽样片段。
- 清单保存原件 ID、哈希、修订、解析 generation、人工修改快照和大小；待确认 24 小时有效。授权后 1 小时有效，只针对该批版本；任一原件、解析或人工内容变化即拒绝执行。
- 只有本人浏览器会话及 CSRF 能授权，且必须确认信息损失。MCP、Skill 和资料正文不能自行授权。网页支持批准并执行、只授权 Agent 执行、取消方案。
- 执行幂等。先撤回访问、使事实支持和依赖页面失效，再排队清理；**实际 unlink 后才释放 used_bytes**。pending_deletion_bytes 解释等待释放的容量。
- 摘要归档在共享目录和空间管理可见，不成为原件证据，也不自动用于个人事实与填表。删除归档同时清除方案中保留的文字。其他未执行方案引用的原件被清理时，也取消并清空预览。
- 没有开启长期自动删除授权或定时监控。网页告警及 Skill 调用时的容量检查已接入；跨会话定时巡检需要独立调度器。

## 验证与交付

检查命令：`uv run pytest`、`uv run python scripts/check_migrations.py`、`npm run build --prefix web`、`uv run python scripts/smoke_workbuddy.py`。

真实本地 HTTP 验收使用 `uv run python scripts/verify_storage.py --large`：只创建独立合成账号和材料，真实传输 2 GiB 零字节数据，验证超配额拒绝、末尾 Range 下载、物理删除后的容量恢复，最后删除该大文件。默认不加 --large 只验收小文件。临时凭据仅保存在忽略目录 work/storage-e2e/session.json（0600），不导出。

结果以本聊天 outputs/storage-and-ocr/verification.json 与验收说明为准。真实 MCP 协议不等于 WorkBuddy GUI 模型调用；本轮未重验 WorkBuddy GUI。仅本地实现、迁移和重启，未发布外网。
