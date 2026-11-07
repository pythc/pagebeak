# 云端文件管理系统选型与接入方案

日期：2026-10-17。

后续实施已完成本地接入，见 [接入与验收](file-management-integration.md)。实际采用自有精简中文 UI 和私有 Cloudreve 存储网关，个人身份、配额和回收站保留在知识库。下文为原始调研，完整网盘 UI、外部账号同步等候选能力不等于已交付。

建议以 **Cloudreve 作为完整网盘的首选验证对象**。它提供独立的网页文件管理界面、多用户、分块上传、预览、回收站及多种存储接入，适合作为知识库的原件管理层。当前服务继续负责解析、原文证据、摘要、知识目录和 Agent 授权流程。正式采用前，须验证单用户配额分配、身份衔接和文件变更同步，不能将“有前端 UI”直接理解为可替换现有后端。

## 候选系统

| 系统 | 用户文件界面与接入能力 | 与本项目的匹配及限制 |
| --- | --- | --- |
| Cloudreve | 完整网盘网页，React 前端；多用户、目录、拖拽与分块上传、预览、回收站；REST API、OAuth、SSE 文件事件；支持本地、COS 和 S3 等存储 | 首选验证。外部 OIDC 登录是 Pro 功能；社区源码的容量上限字段在用户组，任意逐人分配额度须验证或扩展；不能直接把前端当作独立通用组件 |
| Nextcloud | 完整 Files 网页，多用户配额、文件操作、分享与回收站；WebDAV 提供文件 ID、ETag、权限及容量属性 | 适合进一步需要团队协作、同步客户端和应用生态的产品。对于当前个人资料服务，整套平台的身份、应用及运行环境集成成本更高，这是本项目的工程判断 |
| FileBrowser Quantum | 完整网页；OIDC、API Token、用户路径权限；v2.1.0 起支持文件夹、用户 scope 和分享配额 | 轻量备选。官方配额文档仍列出删除计费与索引更新的限制，不适合在未实测前承诺“删除立即释放容量”；主要围绕文件系统 source 工作 |
| 原版 File Browser | 已有网页上传、浏览及编辑 | 排除作为新产品基础。README 已声明归档，不再提供错误或安全修复；不要与 Quantum 分支混淆 |

Cloudreve 的功能与 React 技术栈见 [官方仓库](https://github.com/cloudreve/cloudreve) 和 [前端仓库](https://github.com/cloudreve/frontend)；Nextcloud 的配额与文件操作分别见 [用户手册](https://docs.nextcloud.com/server/latest/user_manual/en/files/quota.html) 和 [WebDAV 文档](https://docs.nextcloud.com/server/latest/developer_manual/client_apis/WebDAV/basic.html)。Quantum 的能力与限制见 [官方项目](https://github.com/gtsteffaniak/filebrowser) 和 [配额文档](https://filebrowserquantum.com/en/docs/features/quotas/)。原版归档状态见 [维护者 README](https://github.com/filebrowser/filebrowser#readme)。

许可证分别为 Cloudreve GPLv3、Nextcloud AGPLv3 或后续版本、Quantum Apache 2.0。二次开发和交付方式需按选定版本核对授权，Pro 能力另行核实；这里不据许可证名称推导具体商用授权结论。[Cloudreve](https://github.com/cloudreve/cloudreve#-license)、[Nextcloud](https://github.com/nextcloud/server/blob/master/COPYING-README)、[Quantum](https://github.com/gtsteffaniak/filebrowser#readme)。

## 用户看到的文件管理页面

产品保留两个互相跳转的入口：“我的文件”呈现原件与用户自己管理的文件夹，“资料目录”呈现知识分类、摘要和原文证据。同一文件可以进入多个知识分类，Agent 的分类变化不应自动改变用户的原始目录。

文件页应具备以下交互：

- 左侧显示全部文件、文件夹、最近使用、收藏、回收站及容量；中间提供文件列表与网格视图，顶部显示面包屑和搜索。
- 支持新建文件夹、重命名、移动、复制、批量选择、上传和下载。保留 2 GiB 产品上限，展示分块上传进度与失败原因。
- 点击文件可查看预览和详细信息，包括大小、更新时间、解析状态、摘要及“查看知识条目”。Office 的预览或编辑服务要分别验证，不能把网盘能保存 Office 等同于可在线编辑。
- 展示总容量、已用和可用空间，临近满时给出扩容或清理入口。清理清单同时呈现要保留的归纳稿、原文引用、删除影响和授权期限。
- 批量操作应报告逐项结果。回收站恢复、永久删除和摘要归档应有明确区别，不能只用一个“删除成功”覆盖所有状态。

可从 [Cloudreve 官方演示](https://demo.cloudreve.org/) 查看产品入口。本次没有登录公共演示或上传用户材料，以上界面要求是针对本项目的接入设计。

## 与知识库连接

建议架构为：用户文件页访问 Cloudreve，知识库通过文件连接服务读取已授权的原件与版本，解析 Worker 产出证据、摘要和目录，WorkBuddy 继续调用当前知识库 MCP。

### 原件与容量由一方管理

如果采用完整 Cloudreve 后端，它应成为相应原件、文件版本与配额的管理方。现有本地原件服务可以在迁移期继续服务旧文件，须为来源记录存储提供方；同一文件不能在两边各保存一份再分别扣减容量。知识库数据库保留原件引用、版本及派生内容，解析时的临时副本完成后清除。

Cloudreve 的文件大小与容量占用不是同一个概念：关联版本和缩略图等 Blob 会影响使用量，无引用 Blob 由后台清理。需要区分用户配额可用量与宿主真正释放的磁盘量，不照搬当前“原件 unlink 后减少本地计数”的实现。[官方文件与 Blob 说明](https://docs.cloudreve.org/en/usage/concept)。

社区版 4.19.1 源码中，`max_storage` 位于 Group，User 保存已用 storage 和所属组；不能据“支持用户容量”推断已有与本项目逐人输入任意配额完全一致的接口。验证时需要选择容量档位分组、确认 Pro 的个人容量能力，或评估扩展；不把为每个人新建一个用户组作为默认长期方案。[Group 定义](https://github.com/cloudreve/cloudreve/blob/4.19.1/ent/schema/group.go)、[User 定义](https://github.com/cloudreve/cloudreve/blob/4.19.1/ent/schema/user.go)。

`GET /api/v4/user/capacity` 已在 4.19.1 路由源码中确认，可作为 Agent 容量查询的适配入口。保留当前 `get_storage_usage` 工具名，但容量口径改由提供方返回；没有明确暴露的预留或待清理明细不能填成 0。管理员页面也要写入同一个容量管理方。[容量接口路由](https://github.com/cloudreve/cloudreve/blob/4.19.1/routers/router.go)。

### 身份绑定与 Agent 权限

现有系统使用自己的邮箱密码和会话，不是现成的 OIDC 身份提供者。Cloudreve 接入外部 OIDC 属于 Pro；它向第三方提供的 OAuth 授权码流程是另一项能力，官方文档给出了用户授权、权限范围与 PKCE。可验证以 Cloudreve 账号作为统一登录入口，或在已有知识库账号下显式绑定网盘账号；最终产品选择一种明确的登录体验。[OIDC 客户端能力](https://docs.cloudreve.org/en/usage/oidc)、[OAuth 授权接口](https://docs.cloudreve.org/en/api/auth)。

绑定使用可信的提供方用户 ID 与本系统用户 ID，不仅凭邮箱匹配。后台保存连接凭据并限制权限；Agent 使用现有 MCP 工具，不能直接获得网盘管理令牌或可绕过清理审批的通用写令牌。Cloudreve 的管理员权限也不能自动变成本系统读取其他用户知识内容的权限。

### 文件变化与索引同步

Cloudreve 官方提供的是 SSE 文件事件，**不是已确认的可靠 Webhook 队列**。事件包含创建、修改、改名和删除，支持临时断连恢复，也可能合并事件。连接服务应使用独立的订阅客户端 ID，先持久化处理任务，再按实际文件状态同步；后台定期核对目录和版本，以补上长时间断线及遗漏，不能仅依赖在线事件。[官方事件协议](https://docs.cloudreve.org/en/api/events)。

| 网盘变化 | 知识库处理 |
| --- | --- |
| 新文件上传完成 | 完成状态核实后读取原件，解析并更新索引、摘要、目录 |
| 重命名或移动 | 更新显示名称和位置；以稳定文件身份绑定，内容未变时不重新提取 |
| 覆盖内容或切换版本 | 旧解析标记过期，按新版本提取；保留人工目录字段 |
| 移入回收站或撤销访问 | 撤回知识库可检索内容及事实支持，不等待物理清理才撤回 |
| 恢复文件 | 重新验证权限与版本，再恢复或重建索引 |
| 永久删除 | 清除对应索引、证据及派生内容，按提供方结果核对容量 |

原件链接、索引引用和授权清单应绑定稳定的文件身份与内容版本，不使用会随改名变化的路径作为唯一主键。SSE 存在同步延迟，因此读取外部来源证据或准备填表时仍要检查当前原件访问权限与版本；外部服务不可用时不把缓存当作已核实的现存证据。

### 原件目录与知识分类

Cloudreve 的目录由用户管理；现有“个人与工作、项目资料、论文与参考文献”等属于知识分类。Agent 可以生成标签、目录建议和归纳稿，用户原始文件夹保持稳定。文件移动不会生成新的知识资料身份，知识分类也不应驱动源文件自动搬动。

### 现有文件迁移

当前原件位于租户和资料 ID 对应的对象路径，文件实体名为 original；该目录并不是用户文件夹结构。不能直接把整个 data/objects 挂到第三方可写文件界面，否则文件名不直观、暂存内容可能被暴露，删除也绕过配额和证据清理流程。

迁移应使用服务端管理清单，逐个上传和核对大小及哈希，建立来源映射；验收完成后切换读取，再按明确清单清理旧副本。期间不改变现有知识资料 ID、人工内容及用户配额归属，不在本次调研直接执行迁移。

## 验证门槛与版本选择

截至本次调研，官方发布页列出的最新版本为 Cloudreve 4.19.1，可作为验证候选，部署时固定版本和校验信息。曾公开的容量并发绕过和 OAuth 管理范围问题，公告列出影响至 4.17.0、在 4.18.0 修复；不要使用早期 v4 示例镜像。这不是完整安全审计结论。[官方版本](https://github.com/cloudreve/cloudreve/releases)、[容量公告](https://github.com/cloudreve/cloudreve/security/advisories/GHSA-xj3h-wwxq-gfcj)、[OAuth 公告](https://github.com/cloudreve/cloudreve/security/advisories/GHSA-w89x-c962-c44g)。

完整 Cloudreve 接入至少需要通过以下验收，才替换现有原件入口：

1. 两个用户隔离，管理员配额调整与 Agent 容量读数一致，明确社区版逐人额度的实现方式。
2. 2 GiB 上传、断线续传、并发超额拒绝和取消后的容量回收。
3. 网页上传后知识库可检索；改名、移动不重复入库；内容覆盖后旧证据不可误用。
4. 文件删除、回收站恢复、永久删除以及跨服务失败时的权限和容量一致性。
5. SSE 断线补偿及重复事件处理；事件与上传客户端不会互相过滤掉必要更新。
6. Agent 只能执行用户批准的具体清理方案，归纳稿保留；没有通用令牌可以绕过该流程。
7. 本地文件迁移保持现有资料 ID 和人工修改，且容量不重复计算。

本轮完成官方文档及相关源码核对，上述实际部署与跨系统验收均为 **NOT_RUN**。系统选型尚未改写为实现决策，也未更换当前服务或文件存储。
