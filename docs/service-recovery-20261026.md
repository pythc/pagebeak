# 2026-10-26 本机服务恢复

用户报告开始页面无法访问。检查时四项服务健康均为 false，知识服务启动器已不在运行，Docker daemon 起初不可达，随后启动。Cloudreve 已随 Docker 恢复，但 PostgreSQL 容器仍为 exited、restart=no。日志可见数据库连接被拒绝后 Worker 退出，原开发启动器再结束其余子进程。未据此推断 Docker 停止的具体原因。

先恢复 PostgreSQL 和应用，让用户页面恢复，再应用以下修复：

- PostgreSQL 增加 `restart: unless-stopped`，与现有文件服务一致；使用原 named volume，应用前保存数据库快照。
- 启动器允许 Docker 的启动等待及 info 超时重试，使用 Compose `--wait --wait-timeout 60`，等 PostgreSQL 健康后才启动知识服务。
- 已管理服务不健康时，start 先恢复容器依赖；若服务已恢复就直接返回。仍异常时给出日志与 restart 指引。
- Worker 对数据库 OperationalError / InterfaceError 做 1、2、4、8、10 秒上限的退避重试，正常执行后重置间隔。事务回滚和任务租约沿用原实现，数据库暂时不可达不当作任务处理失败。ProgrammingError 等编程错误仍会暴露，不无限吞掉。

验证：29 项生命周期、Worker 恢复及业务工作流测试通过，Ruff 通过。实际正常重启 PostgreSQL 容器时观察到 Worker 的数据库重试日志，启动器和 Worker PID 均保持原样，四项 HTTP 健康恢复；验证窗口共 2.76 秒（含恢复后的观察等待，不代表精确停机时间）。两个现有专家 MCP 连接均能读取腾讯目录，27 工具、v4 契约及绑定身份正确。浏览器实际重载开始页，读到原有 5 份资料。

证据及快照保存在 `work/service-recovery-20261026/`；数据库快照为私有文件，不应对外分享。测试只执行正常数据库容器重启，没有调用资料修改/删除接口。

本次未增加 Mac 登录自启动项，也未更改 Docker Desktop 的启动偏好。Mac 整机重启后仍须按教程打开 Docker Desktop，再双击“启动知序.command”；运行中的短时数据库中断现在可自动重试恢复。
