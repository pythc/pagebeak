# Cloudreve 原生前端定制

保留 Cloudreve 原生文件页面，集成知序开始使用和用户指定的侧栏调整。当前本地地址为 <http://localhost:5878/>，开始页为 <http://localhost:5878/start>，腾讯知识目录为 <http://localhost:5878/knowledge>。

## 修改范围

- 2026-10-25：增加腾讯 Memory 的“知识目录”入口。用户与 WorkBuddy 共用条目，支持正文查看、人工修改保护和遗忘；来源变化时展示带警告的历史内容。最新实现见 [切换方案](../../docs/tencent-memory-cutover.md)。

- 在徽标下方、目录树上方增加“开始使用”，原生页面提供当前文件账号的知识库启用/显式关联、WorkBuddy 本机接入和可恢复初始化、添加首份资料引导。忽略其他知识库 Cookie，不按邮箱自动合并。见 [开始页验收](../../docs/native-start.md)。
- 安装与连接就绪后，在同一 WorkBuddy 对话完成需求了解和初始化，可合并询问、批量保存；网页显示进度，重新打开管家的入口收进恢复区，不再作为必需的第二次启动。见 [流程修正](../../docs/onboarding-conversation.md)。
- 修复开始页纵向滚动；Office 默认在本机转换成只读 PDF，并在原生弹窗预览，提供原件下载。Office 预览上限 100 MiB，上传仍为 2 GiB；PDF、图片、文本沿用原生预览。见 [在线预览验收](../../docs/online-preview.md)。
- 移除侧栏入口：视频、音乐、与我共享、我的分享、连接与挂载。
- 左上角使用已有的“知序”叠纸徽标，包含深浅主题资源；窄屏顶栏显示小徽标。
- 基于原生文件夹树显示“目录树”：默认展开我的文件，按需读取子目录，点击名称进入文件夹。
- 补齐多页文件夹、空目录、当前路径展开和同级目录加载；切换账号后丢弃旧请求结果。
- 存储空间单独放在侧栏底部，目录区独立滚动；窄屏侧栏使用可视窗口高度，避免容量卡片跟随菜单浮动。
- 已移除用户侧任务看板入口和页面；旧 /tasks 链接返回“我的文件”。上传后的后台扫描、解析、索引和 Agent 查询接口保留，错误仍由文件操作或 Agent 给出。窄屏上传和删除入口明确显示；已连接账号通过操作报告和后台扫描同步索引，见 [联动验收](../../docs/native-sync-and-tasks.md)。

这棵树展示当前 Cloudreve 账号的实际文件夹层级。知识库的语义“资料目录”仍是另一份用户与 Agent 共用的目录，当前没有把两者合并。移除导航入口不等于禁用分享、音视频文件或 WebDAV 的后端能力。

## 上游与构建

后端固定为 4.19.1，前端固定到后端 assets 子模块对应的 `19da0fe1ecd40971fafa813983d769fdce41573c`。来源、下载地址和归档 SHA-256 见 `upstream.json`。原始代码不提交到本仓库，维护 `frontend.patch` 与新增文件 `overlay/`，解压、依赖与产物保存在忽略提交的 `work/`。

```bash
# 只构建和检查
uv run python scripts/build_cloudreve_frontend.py
# 构建后替换本机 Cloudreve 静态资源，仅重启 Cloudreve
uv run python scripts/build_cloudreve_frontend.py --deploy
uv run pytest backend/tests/test_cloudreve_deploy.py
```

依赖使用上游 `yarn.lock` 与 Yarn 1.22.22；不使用上游陈旧的 npm lockfile。需要 Node、Corepack、Python/uv、curl、patch；部署另需 Docker。

脚本验证归档哈希，将补丁与资源内容生成 revision，比较未改上游和定制版的 TypeScript 诊断，执行 8 项目录分页测试，再使用 Vite 构建。当前上游与定制版均有 **127 条现存 TypeScript 诊断，新增 0 条**；这不是全量类型检查通过，不能将 `build-prod` 的类型检查标为通过。

部署采用 [Cloudreve 官方自定义前端机制](https://docs.cloudreve.org/en/usage/custom-frontend)：将构建产物放入数据卷的 `statics`，不更改后端镜像或数据库。切换前保留旧静态目录，失败恢复逻辑有 4 项命令模拟测试；没有进行真实容器故障注入。成功后需检查 `/version.json` 与页面，浏览器如出现“当前页面有新版本可用”，应点击“更新”刷新 Service Worker 缓存。

## 当前本地版本及回退资料

已部署 revision：`zhixu-b6a26465eda2`（2026-10-25 新手教程入口及腾讯知识目录），包含同一对话接入引导、开始页滚动修复和 Office 本地预览，任务入口与看板继续移除，浏览器已更新并核验。构建、诊断和部署回执在 `work/cloudreve-custom/<revision>/`；`deployment.json` 记录前一版静态资源备份位置，本次为 `/cloudreve/data/statics.backup-20261025141223449443`，不是原版基线。早期静态资源备份仍保留。当前构建执行目录 8 项、拖拽事件 4 项检查；这不是系统文件拖拽的实操证明。详见 [接入流程修正](../../docs/onboarding-conversation.md)、[预览验收](../../docs/online-preview.md)、[开始页验收](../../docs/native-start.md) 和 [任务入口移除](../../docs/task-entry-removal.md)。

需要回退时先保留当前静态目录，再将目标备份切换为 `/cloudreve/data/statics`，仅重启 Cloudreve 并复验。若回到镜像自带的原版页面，将当前 `statics` 改名保留，令该路径不存在后重启即可。回退不删除文件卷、不回退数据库，也不清空浏览器账号数据。

实际界面验收、账号和同步边界见 [验收记录](../../docs/native-frontend-customization.md)。
