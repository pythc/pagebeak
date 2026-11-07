# 计算、字段与实际产物核验

这是一层通用校验，不恢复候选事实库。腾讯知识页用于找资料；填写使用当前原文。每份产物单独 prepare_filling，生成仅覆盖该产物字段的回执。

## 字段契约

先读取实际网页标签、范围和单位；含义不明确时不要默默猜测。fields 每项指定 key、label、operation、value_type、unit、filters 和 rows。
- extract：从引文提取文字或单值数值，不能自动概括、扩写、推定毕业时间。
- sum：Decimal 求和，明确原单位和目标单位；支持元/万元换算，不处理币种。
- count：按 entity_key 去重，value_type=integer，unit=篇/门/项等；不能把检索命中数当统计。
- unknown：rows=[]，提供 missing_reason，输出空字符串；不把未找到解释为零。
- compose：引用同计划的 field_keys，输出保守表述，禁止依赖缺失字段。适合正式材料的事实段；需要自然改写时另作待复核草稿，不能声称自由改写已经过语义认证。

每一行需要有原文真实出现的 entity_key（事项编号或唯一名称）、value、unit、attributes 和 evidence。属性值也必须存在于同一引文，例如年份、负责人、状态。表头与行分开时，在数值行中提供 unit_evidence（第一行表头的 EvidenceAnchor）和 value_column（从 1 开始的列号）。服务核对同一来源版本、同一表/工作表以及同列的实际单元格，不能跨文件拼单位。read_source 返回的 locator.cells 是真实列值，空列也占位置。只支持单行表头；多行/合并表头须人工核实，不能猜测。旧解析缺少 cells 时，用 reparse_source(source_id, expected_revision) 重新解析，等待 get_job 完成后重新读取版本和证据，不沿用旧 ID。不要用随机自造事项编号去绕过重复记录。

完整提交相关候选行，包括重复、他人、计划和排除项，filters 表达任务范围。程序验证已提交记录，不能发现模型漏读的资料。属性未知且可能影响结果时会阻止；明确不符合其他筛选条件的记录可排除。同事项出现冲突数值时回原文核对，不强行修改编号。

示例（用实际 UUID 和版本替换占位符）：
```json
{"request_id":"本次请求UUID","fields":[{"key":"approved","label":"2025年本人主持项目批准经费","operation":"sum","value_type":"number","unit":"万元","filters":{"year":"2025","role":"主持"},"rows":[{"entity_key":"XM25A","value":"82400","unit":"元","attributes":{"year":"2025","role":"主持"},"evidence":{"evidence_id":"原文UUID","source_version":"目录返回的64位版本","quote":"XM25A：2025年，主持，批准经费82400元。"}}]}]}
```

如果表格数字含千分位，保留原样提交；服务返回数值不附单位，因为目标契约已有单位。无量纲排名可用 extract、integer，原/目标 unit 均留空。金额和求和仍须明确单位。返回 invalid_filling_fields 时按 errors 中 field_key 一次修正全部问题，不反复尝试省略单位或改写引文。文本字段不能提交计数；计数不能提交文本类型。不要把批准、到账、支出放在同一统计口径。

## 实际填写

prepare_filling 返回 receipt_id、values、参与/排除/重复事项及原文出处。先检查范围，按 values 填入网页或 Word。所有 values 都是字符串；没有依据的值为 ""。

保存之后重新读取浏览器字段或已落盘 DOCX 段落/单元格，整理成相同 key 集合，调用：
```json
{"receipt_id":"返回的UUID","check":{"artifact":"web_readback","values":{"approved":"8.24"}}}
```
Word 使用 word_readback。missing/extra/mismatched 不为空时修正产物并重新回读。不能将原来 values 对象原封不动提交来假装已读取产物。校验工具本身不控制浏览器，不证明保存动作成功，需要实际回执和读取证据。

新资料、删除或任何原资料版本变化会使旧回执失效；重新读取、计算并新建 request_id。单次重试沿用相同 request_id；内容变化必须换新 ID。回执24小时到期。未知、主观评价、自由文稿不因哈希匹配而成为真实事实。

## 目录出处覆盖

合计、计数的结论须关联所有纳入行的引文和计算回执；不能只引用第一条记录来支持总计。复合结论拆分 statement，或对同一 statement 附多个引用。引用存在不等于它证明了整句结论。人工备注先从目录完整读取，不能根据事实条目的 actor 推断不存在人工备注。

参数位置：evidence、unit_evidence、value_column 只放在 rows 的行对象中，不能放到 fields 的字段对象。金额/单位分列但同一数据行已经包含单位时，直接引用该整行，不传 unit_evidence/value_column；只有单位在表头中才需要表头关联。
