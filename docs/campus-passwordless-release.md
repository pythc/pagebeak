# 免注册教师助理发布说明

教师使用流程：安装 WorkBuddy → 添加知序教师助理 → 按原生提示连接个人空间 → 在首次对话选择检索全部个人文档、指定文件夹或暂不检索。无需新账号、文件密码、手动关联或另装本机客户端。个人空间依靠设备能力凭据与 WorkBuddy 的 OAuth 连接区分，不验证学校身份；不是全校共用一个空间。

教师模型费用归自己的 WorkBuddy 账号。服务器运行文件解析、CPU OCR、关键词索引和存储；WorkBuddy 在线时执行摘要、目录整理和填写。关闭 WorkBuddy 后的语义任务会等待下次对话，不承诺后台持续调用教师模型。全文检索有格式和解析预算，图片型材料需按授权收录后做 CPU OCR。

## 正式发布配置

使用 `deploy/campus/compose.weknora.yaml`，不要复用旧 Tencent 的 `compose.app.yaml`。公网入口沿用已有 Caddy，精确域名为 `zhixu.110-lab.cn`，上游 `127.0.0.1:5879`。API、文件服务、WeKnora、PostgreSQL 均仅回环监听；不开放新的公网隧道。

独立根目录 `/data/agent-knowledge-base`：`releases/<版本>` 为不可变代码和前端，`current` 指向当前版本，`state` 为持久化数据。不要将本机测试账号、合成资料或旧 Tencent 数据自动复制到正式空间。

运行环境参照 `deploy/campus/runtime.env.example`，操作员生成随机凭据到私有文件，不在命令行或日志打印。`state/operator` 为 root 私有目录，数据库管理员凭据仅提供给一次性初始化和迁移进程。API/worker 日常不带迁移凭据，使用无超级用户权限、无 BYPASSRLS 的专用角色。应用数据/私密目录属 UID/GID 10001。

`release.env` 仅含 `ZHIXU_STATE`、`ZHIXU_RELEASE_DIR`、固定 `ZHIXU_RELEASE_IMAGE`。文件服务管理凭据保存在应用私有 `secrets/files-provisioner.json`，仅用于后台自动开通普通空间；不提供给教师或模型。

试点默认 50 个空间，每空间 10 GiB、单文件 2 GiB。服务器原件、衍生索引和临时文件均占用物理磁盘；逻辑配额不是磁盘预分配。保留至少 20 GiB 供解析与系统运行，试点增长前复核全服务器磁盘和负载。Office 在线转换单文件上限仍为 100 MiB，超过时提供原件下载。

## 首次部署顺序

1. 检查目标端口、网段 `10.223.71.0/24`、现有业务负载与目标目录归属；只操作本应用的新目录、容器和服务。
2. 校验发布包和镜像摘要，生成新服务器私有凭据。初始化 PostgreSQL 18 的挂载父目录时，先从固定镜像查询 postgres 用户 UID/GID，并让专用 state/postgres 目录归该用户、模式 0700；只修改本应用目录，不递归修改其他数据。镜像只调整 PGDATA 子目录，挂载父目录若保持 root:root 0700，降权后会无法进入。再启动专用 postgres、files；只在确认新建的空文件卷上允许 `bootstrap_campus.py files --fresh-files`。重试沿用已持久化的随机账号，不重置已有凭据。
3. 一次性执行 `bootstrap_campus.py database` 建立或验证 runtime 角色，再 `alembic upgrade head`。数据库当前目标版本为 0026。迁移失败不启动 API；保留日志和现场，不自动降级。
4. 执行 `bootstrap_campus.py files` 并回读配额、上传限制。`siteURL` 必须与 `AKB_NATIVE_ORIGIN` 的 HTTPS 地址完全一致，避免原件下载被来源校验拒绝。关闭原生公开注册。不得为了通过同步而放宽原件下载来源校验。
5. 安装标准库实现的 `zhixu-preview.service`，预拉取固定摘要的 LibreOffice 镜像。API 只读挂载 Unix socket 目录，不挂载 Docker socket。转换容器无网络、非 root、只读系统，含 CPU/内存/超时限制。
6. 启动 WeKnora、app、proxy；检查 external_executor=true/backend_inference=false、API 健康、数据库只读版本核对、CPU OCR 和 Office 预览。
7. 在原入口配置最新基线上局部合并知序域名，验证后热更新。禁止以旧完整 Caddyfile 覆盖并行改动。核验受信任 HTTPS 和旧站点仍正常。
8. 用校园域名重新执行首次免注册接入、原生 OAuth、网页上传、本机检索、目录与人工修改、实际填写。仅本地通过或健康检查通过不算校园验收通过。

本次运营者确认公开维护邮箱为 `2813994715@qq.com`；仅用于助理包发布者联系信息，教师无需填写邮箱。正式配置不得使用测试占位邮箱。

## 冷备份与恢复

备份目录由操作员创建为 0700，文件为 0600。先停止本应用 proxy，暂停写入入口；停止 app、files、WeKnora 后，对专用 PostgreSQL 执行 `pg_dump -Fc`，同时备份 `state/{files,weknora,objects,secrets,work,operator}` 与 `release.env`、当前发布摘要。不要把在线 PostgreSQL 数据目录的普通 tar 当有效数据库备份。

备份后记录校验和，启动被暂停的服务并确认健康。备份包含随机空间身份、OAuth 状态、私有文件服务账号和 WeKnora 密钥；泄露这些等于泄露资料空间访问权，不能提供给模型或公开下载。

恢复先在独立数据库和新数据目录操作，恢复完整一致的文件及密钥，再检查已有空间、旧资料检索、目录、回执失效和原件下载。不得只恢复 SQL 而遗失文件账号或 WeKnora 密钥，也不能在仍运行的目录上直接覆盖恢复。恢复到新域名需要显式调整服务地址绑定并重新授权，不能用新域名静默接管旧 OAuth grant。

## 升级与回退

升级前冷备份，保留旧发布目录和镜像摘要。新版本先验证迁移与数据兼容，再切换 current 并重建本应用容器。前端构建与后端一起固定版本，不能只改软链接却继续挂载旧前端。

初次发布失败时停掉本次知序路由与服务，保留数据目录、DNS 和旧业务。后续回退只在旧代码支持当前 schema 时切回；0025 之后的设备/空间绑定不自动删除，不以强制 downgrade 消除版本检查。若须恢复备份，应先审阅备份后新增数据和恢复范围，不能默默丢弃用户资料。Caddy 回退只移除本次精确主机片段，不回退其他站点的并行改动。

本说明是部署流程；实际执行状态以同批验收报告为准，不能将流程描述作为已部署证据。
