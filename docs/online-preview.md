# 在线预览与开始页滚动修复

日期：2026-10-17。用户反馈开始页不能下滚，并要求在文件空间内预览常见文件。

## 行为

- 开始页拥有独立的可滚动内容区，保持侧栏与顶栏；鼠标滚轮、触控和键盘可以到达底部，不再把内容撑高后裁掉。
- 在文件列表双击 Word、Excel、PPT，默认打开原生预览弹窗。在本机把原件转换成只读 PDF，使用已有中文 PDF.js 阅读器展示，可翻页、缩放、查找和下载原件。不会改写原件或把预览副本入库。
- 本地 Office 转换支持 doc/docx、xls/xlsx、ppt/pptx、odt/ods/odp、rtf；PDF、图片、Markdown 和文本继续使用原生预览器。格式支持不等于本轮逐项实测。
- PDF 默认直接打开内置阅读器，不先询问选择微软、谷歌或其他应用；显式“打开方式”仍沿用原生选项。
- 关闭、切换文件或切换账号会丢弃旧响应并释放浏览器 Blob URL。加载时允许关闭；失败展示重试和原件下载，不再直接关闭窗口隐藏错误。
- 初始隐藏 PDF 侧栏、适合页宽，避免窄屏正文被侧栏遮住。Office 明确标记只读；复杂布局、字体替换、公式结果、动画与打印区域以原件为准，不提供在线 Office 编辑。

## 接入选择

Cloudreve 自带微软/谷歌公共 Office 阅读服务，但要求公网地址和标准端口，本机私有文件不能直接使用；见 [官方文件应用说明](https://docs.cloudreve.org/en/usage/file-apps)。本次复用已固定镜像里的 LibreOffice 和中文字体，本地生成 PDF，没有增加向外部 Office 服务传文件的流程。协同编辑未来可独立评估 [官方 WOPI 集成](https://docs.cloudreve.org/en/usage/wopi)。

使用 [LibreOffice headless 转换接口](https://help.libreoffice.org/latest/en-GB/text/shared/guide/start_parameters.html)。每次转换是独立的非 root 容器：无网络、只读镜像、移除 capabilities、禁止提权，仅挂载该文件的只读输入与临时输出/配置，不挂载文件服务卷、Docker socket、用户主目录或凭据。独立用户配置设定最高宏安全级别且不配置可信目录；安全级别含义见 [官方说明](https://help.libreoffice.org/latest/en-GB/text/shared/optionen/macrosecurity_sl.html)。

## 权限与资源边界

新增 `POST /v1/native/preview`。使用当前文件账号的临时访问令牌调用 Cloudreve 核验身份、文件权限、ID 和最新版本，忽略知识库 Cookie，不要求先关联知识库。只接受当前 `cloudreve://my/` 下的 Office 文件，不接受客户端提供的任意下载 URL。原件链接仅允许配置中的文件服务域名、不跟随重定向、不向下载地址转发 Bearer。下载前后及转换完成后重新检查文件版本，变更或删除时拒绝返回旧内容。

Office 单文件预览限制 100 MiB，输出 64 MiB，并发 2 个，转换超时 90 秒，单容器 768 MiB 内存与 1 CPU、128 个进程。超时也清理容器；请求结束清理原件和生成文件，响应 `no-store`，不形成永久缓存或占用用户网盘配额。2 GiB 上传限制保持不变。大文件仍可上传、下载，不能把上传上限等同于浏览器预览上限。

当前转换依赖同一台开发机的 Docker CLI 和本地已有的固定 Cloudreve 镜像；API 不在容器内部访问 Docker socket。生产应改为独立的受控转换 Worker，而不是向普通 Web 容器授予 Docker 管理权限。当前原件下载只验证了本地存储，外部存储域名需要单独接入。

## 验证

- 后台全量 **134 项测试通过**，其中 12 项新增用例覆盖身份/来源、其他空间、外部 URL 拒绝、限额、版本变化、转换后变化、临时文件清理、非 root 隔离参数和超时清理。Ruff 通过。
- 原生前端 Vite 构建通过；上游/定制 TypeScript 诊断 127/127，新增 0。目录 8 项、拖拽事件 4 项检查通过。
- 实际原生文件账号的 Word、Excel、PPT 各返回有效 PDF，均 1 页、中文文本可抽取；另用已有合成资料转换 Word 1 页、Excel 2 页、PPT 2 页，验证多页输出。没有用文本摘要冒充原版式预览。
- 实际浏览器显示 Word 正文、Excel 单元格、PPT 页面和扫描 PDF 图像；最终版本确认 PDF 双击直接预览、Word 预览与原件下载。浏览器“下载原件”得到 36,878 字节的 docx，与服务端原件 SHA-256 一致。没有把 PDF.js 的“保存”当成原件下载。
- 本机部署 revision `zhixu-956968348a01`，浏览器实际脚本 `/assets/index-ksr8niVB.js` 与构建一致；前一版静态资源保留于 `/cloudreve/data/statics.backup-20261017073953189458`。
- 开始页 574×808 视口实操滚动，内容区由 scrollTop 0 到约 289，末条示例文字位于可见窗口内；桌面与文件预览实操结果、截图见本聊天 `outputs/preview-and-scroll-20261017/verification.json`。

自行 review 重点为下载权限、重定向、宏/网络隔离、资源上限、临时文件清理、转换期间版本变化、前端旧响应和 Blob URL 释放。未改变数据库结构或 MCP 工具数量，未提交、推送、部署生产或修改用户原件。

`NOT_RUN`：真实 100 MiB/2 GiB Office 压力测试、恶意宏样本、生产 Docker 故障注入、旧版二进制 Office/开放文档全部格式逐项浏览器验证、远程存储、Office 在线编辑。图片、文本与 Markdown 本轮没有新增能力，也不以 Office 用例代表它们已重新全量验收。
