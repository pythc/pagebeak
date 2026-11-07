# 2026-10-25 本机部署与交接

新版部署于当前 Mac，仅监听本机。用户从 http://localhost:5878/start 进入，教程为 http://localhost:5878/zhixu-guide.html ，知识目录为 http://localhost:5878/knowledge 。未部署公网服务器，未发布 WorkBuddy 市场插件。

## 当前版本

- 腾讯 Memory + 当前 WorkBuddy 模型；接入包 0.7.1、知识契约 tencent-workbuddy-v4、27 个 MCP 工具。
- 前端 revision `zhixu-b6a26465eda2`，仍使用固定 Cloudreve 4.19.1 后端镜像。开始页增加新手教程，教程由 `docs/新手使用教程.md` 在构建时生成。
- 数据库已在 0020，无本次新增数据结构迁移。旧知识数据与人工修改保留。
- 两个已安装的自有专家通过现有配置启动真实 MCP 新进程，身份绑定正确、27 个工具、腾讯目录可读。没有输出或更改它们的凭据、连接器配置或信任记录。

## 用户如何打开

先打开 WorkBuddy。再打开“开始使用”，按页面连接或恢复当前管家对话；已有连接在 WorkBuddy 中关闭再开启一次以刷新工具。刷新后应为 27 个工具。当前窗口自动化返回窗口不可用，未自动完成客户端这个动作；协议新进程验证与客户端缓存刷新分别记录。

从第一份资料到填写 Word 的完整步骤见 [新手教程](新手使用教程.md)。同一对话允许合并询问和批量保存，无需回网页再点击初始化。

## 日常启动和维护

这次已启动后台服务。以后重启电脑，先打开 Docker Desktop，再双击仓库根目录 `启动知序.command`。脚本启动容器和知识服务，窗口关闭后服务继续运行；没有添加登录自启动项。

```sh
cd /Users/alex/workspace/agent-knowledge-base
.venv/bin/python scripts/zhixu.py status
.venv/bin/python scripts/zhixu.py start
.venv/bin/python scripts/zhixu.py restart
.venv/bin/python scripts/zhixu.py stop
```

stop 只停止本入口管理的 API、Worker 和腾讯子进程，Cloudreve/PostgreSQL 容器继续运行。不要同时执行旧开发启动器。运行状态和日志位于 `data/runtime/`；重复 start 不再创建进程；PID 被复用或身份变化时拒绝结束该进程；部分外部服务占据端口时拒绝接管。

更新教程后正常运行 `scripts/build_cloudreve_frontend.py --deploy`，构建器先重新生成教程；页面出现“当前页面有新版本可用”时点击“更新”。

## 本次实际验证

- 保存配置、PostgreSQL、腾讯数据和本地 objects 的升级前快照，原启动器正常停止后采集；未复制外部 Cloudreve 文件卷，不能称为全产品灾备备份。
- 运行时依赖与 SQLite FTS5 检查通过；数据库 0020；固定入口 start、重复 start、restart、status 的实际运行均通过，四项健康状态正常。
- 新生成的包经 WorkBuddy 自带专家校验器及隔离安装验证，真实 MCP 27 工具、首次绑定、同一请求保存三项初始化回答、重开恢复通过，测试凭据撤销。
- 两个现有专家真实 MCP 进程核验通过；不读取或更改用户原件。
- 24 项相关回归通过，Ruff 通过。前端固定上游/定制 TypeScript 诊断 127/127，新增 0；目录树 8 项、拖拽逻辑 4 项通过；不声称上游所有类型错误均已清零。
- 浏览器正常点击新版本更新、从开始页打开教程、复制示例显示成功、到达教程末尾；721×988 窗口无横向溢出。教程与开始页保留为可查看标签。
- 本次未新跑一轮真实模型访谈或填表。上一轮真实 Word/网页验收仍见 [同资料验收](memory-ab-acceptance-20261025.md)，不把协议检查冒充新的模型验收。

完整机器回执、前端构建日志、截图与备份位置见 `work/deployment-20261025/release.json`。其中 private 目录不对外分享。

## 回退

先正常停止知识服务并保留当前数据。前端上一版本位于 `work/cloudreve-custom/zhixu-b6a26465eda2/deployment.json` 指定的 previous_statics；保留当前 statics，切回该目录后只重启 Cloudreve。不要删除文件卷。

本次仅新增生命周期入口、教程及验证脚本，未改变已验收的知识业务引擎，也不需要数据库降级。开发人员可在停止本入口后使用原 `scripts/dev.py` 启动。不要混用两个启动器或把历史工作目录的 PID 文件当成当前运行状态。恢复历史快照会覆盖后续写入，必须先备份并明确恢复范围；本次未执行覆盖式回滚。
