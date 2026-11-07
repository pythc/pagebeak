# 空间维护与清理

用 get_storage_usage 读取 quota_bytes、used_bytes、reserved_bytes、available_bytes。配额按原件字节计，上传预留整份文件容量；派生数据库内容暂不计入配额。near_full 表示已用和预留合计达到 80%，95% 为紧张。一次任务内无变化时不重复提醒。

空间不足时先说明剩余容量，给出清理或管理员扩容选项。list_cleanup_candidates 按大小返回候选；文件大、旧或名称相近都不证明无用或重复。结合用户当前用途判断，不为了释放容量牺牲有价值的原始证据。

用户愿意清理后，用 propose_storage_cleanup(source_ids, mode) 创建可审阅清单：

- delete：删除指定原件，依赖的证据、个人事实支持和页面会失效。
- summarize：先生成跨全文的原文抽样摘要，保留人工目录修改，再删除原件。这是有损归档，不是无损压缩，不能保证保留所有条款、数字、公式和附件。

需要主动归纳时，先分页 read_source 读取相关文件，再给 propose_storage_cleanup 传 summaries：每项包含 source_id、content（归纳稿）与 citations（evidence_id、quote）。原文引文必须逐字匹配该文件当前证据；服务端校验引用归属和版本，但不能证明整段归纳的语义准确。保留不确定项、原件解析缺口和关键信息，不编造缺失数字；用户在网页审阅这份归纳后再授权。未传 summaries 时后台只做原文抽样，不得称为 Agent 已完成语义归纳。

向用户展示文件、预计释放量、损失及摘要，引导到网页“空间管理”批准并执行，或授权 Agent 执行该批方案。当前 MCP 没有授权工具，不能自行批准，也不使用 shell/数据库/浏览器绕过确认。授权绑定清单、原件版本、解析版本和人工修改；只对这批文件生效，1 小时后失效，不等于长期自动删除许可。

用 read_storage_cleanup 检查 authorized 后，才调用 execute_storage_cleanup。同一 plan_id 可安全重试；403 不绕过，409 表示过期或资料变更，重新提出方案并让用户审阅。取消的方案不得执行。执行返回 cleanup=queued 时不要声称已释放空间；再用 get_storage_usage 核对实际已用量。

read_storage_archives 返回经用户批准保留的有损归档，也可从共享目录找到。原件已删后不可伪造下载链接或把归档当作原件证据；它不参与个人事实采纳和自动填表。用户可在空间管理删除归档及方案中留存的片段。

Skill 仅在被宿主调用时检查容量；网页也显示容量告警。没有真实调度器时，不宣称已开启全天监控或自动清理。
