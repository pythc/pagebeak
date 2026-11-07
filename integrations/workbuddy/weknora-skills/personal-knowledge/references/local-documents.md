# 本机与云端联合检索（无需另装客户端）

教师只安装 WorkBuddy 和本助理。`scripts/host_documents.py` 与 `scripts/pypdf.zip` 随 Skill 提供，使用 WorkBuddy 自带 Python；不要求教师安装 Python、pip、数据库、OCR、额外 MCP 服务或常驻程序。远程 MCP 不能读取教师本机路径。此脚本只运行在教师当前 WorkBuddy 所在电脑，不能在校园服务器上运行冒充教师设备。

## 首次启动与范围

先 `get_knowledge_context` 固定本人库 ID 和 local_documents.api_origin，读取本参考。检查本机 Skill 的 scripts/host_documents.py 与 `--help`；脚本缺失或没有 status 命令时，使用宿主正常助理更新流程安装 0.13.9 或更新版。只刷新远程工具不会更新本机脚本。检查宿主提供的 Python 是否可执行（需要 Python 3.10+ 和 sqlite3/FTS5）。优先使用宿主明确提供的运行环境或正常 PATH；macOS 的 WorkBuddy 运行时常位于 `~/.workbuddy/binaries/python/envs/default/bin/python`，先确认文件存在。其他系统按宿主提供的路径执行。运行环境不可用时保留云端功能并如实报告，不能静默安装依赖、关闭沙箱、改 PYTHONPATH 或使用 -S 绕过拦截。

下列 `PYTHON SCRIPT --library LIBRARY --origin ORIGIN` 均替换为已确认路径、库 ID 和服务根地址。shell 参数要正确引用，不将令牌或引文拼接进命令。许可凭据只放权限受限的临时 JSON；不输出、不写对话总结。Python 脚本无后台模型调用。

日常使用保留脚本默认的用户私有缓存位置，不传 `--state` 将索引改放到某次对话的工作目录。默认缓存按服务地址和库 ID 隔离，便于新对话继续使用同一设备索引。默认位置不可写时报告具体权限问题，不关闭沙箱；如用户已指定其他持久位置，应跨对话复用该位置，不能每次建立新设备清单。

先根据用户选择将配置写到私有 JSON 文件，调用 `configure --settings CONFIG`；CONFIG 是该文件的路径，不是内联 JSON 字符串。配置示例：

```json
{"user_authorized":true,"enabled":true,"roots":["用户选择的绝对资料目录"],"all_documents":false,"auto_import":false,"share_catalog":false,"publish_summary":false}
```

- 只使用可信对话里的授权。没有选择则不开启；关闭用 enabled=false。首次优先询问检索范围；身份、用途不作为开始使用的必填项。
- 用户明确选择全部文档才 all_documents=true：覆盖个人主目录内支持的非隐藏文档，排除系统、缓存、凭据、依赖及链接。不自动包含其他账号、外置盘或云盘未下载原件。
- auto_import 默认 false；用户允许自主挑选重要资料入库时才启用。不启用时仍可检索，但每次收录必须有指定原件或明确用途授权。
- share_catalog 默认 false；开启会同步文件名、相对目录、大小、状态、哈希，不同步正文和绝对路径。已收录文件的版本观察始终需要同步，以阻止旧内容填写。
- publish_summary 单独记录本机扫描范围的概览保存授权。没有明确授权保持 false；同一范围可沿用选择，换范围须按该范围的授权配置。执行 status 查看当前配置、索引汇总、最近一批回执和是否还有批次待续。已存在的旧索引可能没有最近扫描回执。

范围确定后运行 `scan --seconds 25`。遇到 time_budget 用相同命令续扫，visited 和 changed 是本轮累计值，后续批次从保存的游标继续。上一次完成的目录在下一次新扫描中增量检查；本轮扫描开始后在游标前新增或修改的文件由下次增量发现。file_limit 表示本轮达到 1 万份上限；directory_unavailable 表示目录离线或读取出错。遇到这两种情况，或者连续批次没有进展，如实报告部分覆盖，停止重复重试。complete=true 表示枚举结束，正文覆盖以 status_counts 和 indexed_partial 为准。单文件解析预算 128 MiB、50 万字符、18 秒，PDF 最多 40 页。Word、Excel、PPT 可提取文字、单元格与公式缓存；图片、图表和批注不保证覆盖。只有文件名可检索的材料可按需收录后使用服务器 CPU OCR 或转换。

扫描后按 [扫描后的进度与资料概览](local-overview.md) 写入并回读。读取索引状态即可复用仍可用的缓存，正文在实际使用时重新核对。

## 联合检索和收录

1. 用 `search QUERY` 和 MCP `search_library` 分别查询本机、云端。关键词检索可改写同义词补查；命中数不是成果数量。`catalog --offset N` 查看本机清单，`read --id ID --hash HASH --offset N` 读当前原文；核对姓名、金额、时间和角色。每条结果区分本机未收录/云端原件/知识页。
2. 正式知识页或填表用到本机原件时，先确认在授权收录范围内，再运行 `upload-request --id ID --hash HASH --scope self|reference`。把返回对象作为 `prepare_host_file_upload(upload=...)` 参数；收藏论文用 reference，不能当作本人论文。上限和剩余空间先 `get_storage_usage` 检查。
3. 将 MCP 返回完整许可写私有临时 JSON，运行 `transfer --ticket FILE`。脚本只访问许可绑定的同一服务、同一文件，不读取其他云端资料，不接受重定向；这项狭窄传输是主技能禁止直接 HTTP 代做的唯一例外。传输成功自动删除临时许可；失败保留用于短期续传，别展示凭据。同一请求重试不得换文件/哈希/归属，过期则重新申请。
4. 等 `get_job` 完成，必要时 `sync_library`，通过 `read_source/read_evidence` 获得正式出处。随后执行主技能中的任务领取、目录整理、`prepare_filling/validate_filling`；本机缓存摘录不能代替云端证据 ID。
5. `export-sync` 返回 request 和私有清单路径，用 `prepare_host_catalog_sync(sync=request)` 签发许可，再 `transfer --ticket FILE`。发生409先 `read_shared_local_catalog(device_id=...)` 读最新 revision，核对设备后 `export-sync --revision N` 重导出。不能盲目覆盖其他设备清单。成功后用户与 Agent 读取同一份网页目录。

## 增量、人工修改与撤销

继续本机资料相关任务时检查 status，按需续扫或增量 scan；`changes --after N` 查看文件变化。已收录文件的版本观察或已获准的文件清单通过 export-sync/transfer 同步。哈希不符必须先扫描、同步失效标记，再收录当前版本。同步失败不得用旧原件做当前填写。只重整受影响主题，保留 protected 人工页；人工页依赖失效时说明需要复核，可补充有新出处的关联页。

本机缺失/离线不自动删除云端快照；撤销目录立即停止读其正文，已收录云端资料仍保留。改名、复制各留目录条目，云端按文件哈希和归属去重。WorkBuddy 未运行期间不扫描、不做模型整理，下次使用再发现变化。不要把清单最近同步时间解释为当前在线；重要原件正式填写前再次检查本机和云端版本。
