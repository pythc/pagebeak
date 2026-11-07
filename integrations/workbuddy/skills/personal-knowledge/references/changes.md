# 文件变化与 Agent 检查点

这些工具需要更新后的 personal-knowledge MCP。若宿主仍缓存旧工具列表，引导正常重连；不假装已经检查，也不改用其他账号。

- `get_sync_state` 中，`current_hash` 是最近观测的文件状态，`indexed_hash` 是索引和目录完成后的版本。`agent_observed_hash` 和 `agent_change_cursor` 单独记录本令牌上次确认的位置；后台整理不会替 Agent 确认。两个文件哈希相同，也可能仍有未读历史。
- `agent_needs_review` 为真时，以 `agent_change_cursor` 调用 `read_file_changes`，按 `next_cursor` 分页到当前 `latest_change_id`。同一文件先改后恢复，最终哈希可能相同，仍要读取中间操作。`browser`/`api` 是知识库调用凭据类型；原生文件空间的 `frontend_observed`/`external` 只是观测来源，不是对真人操作者的身份认证。
- 按任务需要，用 `read_file_change_content(event_id, version=before/after)` 分页比较修改前后的已解析文本。标记为 historical 的内容只用于理解差异，当前结论仍回到有效的 read_source/read_evidence；撤回资料不会提供历史正文。不虚构原始文件版本恢复能力。
- 如需立即核对原生文件，调用 `sync_file_changes`，查询返回的任务。`scan_pending`、`scan_error` 或 `needs_update` 尚未解除时，报告真实进度，不宣称同步完成。不要因一次暂未完成而重复导入文件、覆盖人工字段或连续排队；已有任务优先等待。目录暂停设置保持不变。
- 读完这一批历史、核实相关索引和目录后，以刚读取的 `current_hash` 和 `latest_change_id` 调用 `acknowledge_file_changes`。它只推进本令牌的确认检查点，不修改文件；409 表示状态又变了，应重新读取，不能强行覆盖。此游标由服务保存，重启宿主仍可续读；换令牌有独立检查点。

自动解析与目录更新由后台执行，不代表 Agent 已在场阅读或完成语义判断。整理结果保留人工目录标题、摘要和备注；删除或恢复仍依照实际授权和产品能力。
