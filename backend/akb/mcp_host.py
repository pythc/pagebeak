"""Transfers for a teacher-side Skill; never reads paths on the server."""
from uuid import UUID

from akb import mcp_server as files
from akb.host_transfer import HostSync, HostUpload


def prepare_host_file_upload(upload: HostUpload) -> dict:
    """为 Skill 本机脚本签发单文件上传许可，不读取服务器本地路径。
    先按 references/local-documents.md 扫描并 upload-request 得到 upload。
    用户指定收录或已授权自动收录才调用。返回许可写私有临时 JSON 后交脚本 transfer；勿展示凭据。
    请求必须包含 SHA-256、大小、设备与文档 ID。它不能调用其他 API，20 分钟后失效。
    """
    return files.call("POST", "/v2/host-transfers/upload", upload.model_dump(mode="json"))


def prepare_host_catalog_sync(sync: HostSync) -> dict:
    """为 Skill 导出的清单及已收录文件变化签发哈希绑定许可。
    sync 使用 export-sync 返回的 request；仅在用户选择共享清单时同步名称，原件变化观察用于使旧版本失效。
    返回许可写私有临时 JSON 后执行 transfer。409 须 read_shared_local_catalog 核对修订号后重新导出。
    """
    return files.call("POST", "/v2/host-transfers/catalog", sync.model_dump(mode="json"))


def read_shared_local_catalog(device_id: str | None = None, offset: int = 0, limit: int = 50) -> dict:
    """读取与用户文件页相同的本机文件清单及设备修订号；清单不代表原件已经上传或当前在线。"""
    device = "/" + str(UUID(device_id)) if device_id else ""
    return files.call("GET", f"/v2/local/catalog{device}?offset={max(0, offset)}&limit={min(100, max(1, limit))}")
