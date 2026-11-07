"""Bounded local stdio file upload; the host must stage only the user's authorized file."""

import fcntl
import hashlib
import mimetypes
import os
import re
import stat
from pathlib import Path
from uuid import UUID

from akb.workbuddy_entry import private_directory

MAX_FILE_BYTES = 2 * 1024**3
MAX_CHUNK_BYTES = 8 * 1024**2


def inbox():
    try:
        kb = str(UUID(os.environ.get("AKB_EXPECTED_KB_ID", "")))
    except ValueError:
        raise ValueError("上传前必须固定本人的知识库身份") from None
    root = Path(os.environ.get("AKB_FILE_INBOX_ROOT", str(Path.home() / ".zhixu-file-inbox"))).expanduser()
    if not root.is_absolute():
        raise ValueError("本机文件收件目录必须使用绝对路径")
    private_directory(root)
    private_directory(root / kb)
    return root / kb, kb


def prepare(api):
    context = api("GET", "/v1/context")
    expected = os.environ.get("AKB_EXPECTED_KB_ID", "")
    if not expected or context["knowledge_base_id"] != str(UUID(expected)):
        raise ValueError("上传目标与本连接固定的知识库不符，已停止")
    usage = api("GET", "/v1/storage")
    directory, kb = inbox()
    return {
        "knowledge_base_id": kb, "staging_directory": str(directory),
        "upload_limit_bytes": min(MAX_FILE_BYTES, usage["upload_limit_bytes"]),
        "chunk_bytes": min(MAX_CHUNK_BYTES, usage["chunk_bytes"]),
        "available_bytes": usage["available_bytes"], "near_full": usage["near_full"],
        "next": "用宿主正常文件工具，将用户明确授权收录的那个文件复制到收件目录的直接子文件，"
                "再调用 upload_file。不要扫描磁盘、放置链接或改保护设置；无法复制就说明限制。",
        "local_stdio_only": True,
    }


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def unchanged(api, stream, before, upload_id=None):
    if signature(os.fstat(stream.fileno())) == signature(before):
        return
    cancelled = False
    if upload_id:
        try:
            cancelled = api("DELETE", f"/v1/uploads/{upload_id}")["status"] == "cancelled"
        except ValueError:
            pass
    suffix = "；该次上传已取消，预留容量已释放" if cancelled else "；请查询并取消该次未完成上传" if upload_id else ""
    raise ValueError("本机暂存文件在上传过程中发生变化，已停止；重新复制后请使用新的 request_id" + suffix)


def transfer(api, stream, before, name, request_id, subject_scope, folder_id, limits):
    digest = hashlib.sha256()
    while data := stream.read(1024 * 1024):
        digest.update(data)
    unchanged(api, stream, before)
    sha256 = digest.hexdigest()
    body = {"filename": name, "media_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "size": before.st_size, "subject_scope": subject_scope, "folder_id": folder_id, "sha256": sha256}
    started = api("POST", "/v1/uploads", body, request_id)
    upload_id = str(UUID(started["id"]))
    try:
        if started["status"] not in {"staging", "completed", "settled"}:
            raise ValueError("原上传已取消或过期，请使用新的 request_id")
        offset = started["offset"]
        if not isinstance(offset, int) or not 0 <= offset <= before.st_size:
            raise ValueError("服务返回了不合法的上传位置")
        chunk_bytes = limits["chunk_bytes"]
        if not isinstance(chunk_bytes, int) or chunk_bytes < 1:
            raise ValueError("服务返回了不合法的分块限制")
        recoveries = 0
        while offset < before.st_size:
            unchanged(api, stream, before, upload_id)
            stream.seek(offset)
            data = stream.read(min(chunk_bytes, before.st_size - offset))
            if not data:
                unchanged(api, stream, before, upload_id)
                raise ValueError("暂存文件未读到预期长度")
            try:
                received = api("PUT", f"/v1/uploads/{upload_id}/content?offset={offset}", content=data)
            except ValueError as exc:
                # An acknowledged chunk can be committed even when its response was lost.
                if not hasattr(exc, "status_code") or exc.status_code not in {None, 409} or recoveries >= 2:
                    raise
                recoveries += 1
                current = api("GET", f"/v1/uploads/{upload_id}")
                next_offset = current["offset"]
                if (current["status"] not in {"staging", "completed", "settled"}
                        or not isinstance(next_offset, int) or not offset <= next_offset <= before.st_size):
                    raise ValueError("上传状态已改变，请查询后再继续") from None
                offset = next_offset
                continue
            if received["offset"] != offset + len(data):
                raise ValueError("服务返回的上传位置与已发送分块不一致")
            offset = received["offset"]
        unchanged(api, stream, before, upload_id)
        result = api("POST", f"/v1/uploads/{upload_id}/complete")
        if result["source"]["sha256"] != sha256 or result["source"]["size"] != before.st_size:
            raise ValueError("服务返回的原件摘要或长度与本机文件不符，请停止使用该结果")
        return {**result, "upload_id": upload_id, "sha256_verified": True,
                "processing_status": result["source"]["status"],
                "next": "原件已接收，使用 get_job 等待解析，再读取共用目录和原文核验；不要将接收成功说成可检索。"}
    except (ValueError, OSError) as exc:
        raise ValueError(f"上传尚未完成或未通过核验（upload_id={upload_id}）。"
                         f"沿用原 request_id 与同一文件重试可续传，或用 cancel_file_upload 取消。原因：{str(exc)[:350]}") from None


def upload(api, local_path, request_id, subject_scope="reference", filename=None, folder_id=None):
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", request_id):
        raise ValueError("request_id 请使用不超过 120 字符的 UUID 或英文数字标识；同一次上传重试保持不变")
    if subject_scope not in {"self", "reference"}:
        raise ValueError("资料归属只支持 self 或 reference")
    if folder_id:
        folder_id = str(UUID(folder_id))
    limits = prepare(api)
    directory, _ = inbox()
    path = Path(local_path)
    try:
        relative = path.relative_to(directory)
    except ValueError:
        raise ValueError(f"只接受本库收件目录中的文件。先由宿主正常文件工具复制指定文件至 {directory}，不能直接读取其他路径") from None
    if not path.is_absolute() or len(relative.parts) != 1 or relative.name.startswith("."):
        raise ValueError("请提供收件目录直接子文件的绝对路径，不支持隐藏文件、子目录或路径跳转")
    name = filename or path.name
    if (not name or len(name) > 255 or name in {".", ".."} or "/" in name or "\\" in name
            or any(ord(c) < 32 for c in name)):
        raise ValueError("原文件名不合法")
    locks = directory / ".locks"
    private_directory(locks)
    lock_path = locks / (hashlib.sha256(request_id.encode()).hexdigest() + ".lock")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        lock_info = os.fstat(lock.fileno())
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
            raise ValueError("上传锁文件异常，已停止")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("同一 request_id 的上传正在进行，请等待或查询原上传，不要并发重试") from None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            raise ValueError("无法读取收件目录中的普通文件，请检查文件是否存在或是否为链接") from None
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(descriptor)
            raise ValueError("只支持普通文件，不接受目录、符号链接、硬链接或特殊设备")
        with os.fdopen(descriptor, "rb") as stream:
            if not 0 < info.st_size <= limits["upload_limit_bytes"]:
                raise ValueError("文件不能为空，且单文件不能超过 2 GiB 上限")
            return transfer(api, stream, info, name, request_id, subject_scope, folder_id, limits)
