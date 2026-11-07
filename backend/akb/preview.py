"""Read-only native Office previews in a disposable, credential-free container."""

import os
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import Header, HTTPException, Response
from pydantic import Field

from akb import native, office_conversion
from akb.office_conversion import INPUT_LIMIT, OFFICE_EXTENSIONS, OUTPUT_LIMIT
from akb.schemas import Input


class PreviewInput(Input):
    uri: str = Field(min_length=1, max_length=4096)
    file_id: str = Field(min_length=1, max_length=100)
    version: str | None = Field(default=None, max_length=100)


def convert_document(directory: Path, extension: str) -> bytes:
    try:
        return office_conversion.convert_document(directory, extension)
    except office_conversion.ConversionError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


def convert_remote(socket: Path, original: Path, extension: str) -> bytes:
    # Only bytes and an allowlisted suffix cross the host boundary, never a path
    # or the caller's credentials. The socket is mounted read-only in the API.
    transport = httpx.HTTPTransport(uds=str(socket))
    with httpx.Client(transport=transport, timeout=110, trust_env=False) as client:
        with original.open("rb") as body, client.stream("POST", "http://preview/convert",
                headers={"Content-Type": "application/octet-stream", "X-Document-Extension": extension,
                         "Content-Length": str(original.stat().st_size)}, content=body) as response:
            if response.status_code != 200:
                code = response.status_code if response.status_code in {413, 415, 422, 429, 503, 504} else 503
                raise HTTPException(code, "Office 预览暂不可用，请稍后重试或下载原件")
            output = bytearray()
            for chunk in response.iter_bytes(1024**2):
                if len(output) + len(chunk) > OUTPUT_LIMIT:
                    raise HTTPException(422, "预览结果超过大小限制，请下载原件")
                output.extend(chunk)
            if not output.startswith(b"%PDF-"):
                raise HTTPException(422, "转换没有生成有效 PDF，请下载原件")
            return bytes(output)


def render(settings, token: str, body: PreviewInput) -> bytes:
    uri = urlsplit(body.uri)
    if uri.scheme != 'cloudreve' or uri.netloc != 'my' or not uri.path.startswith('/') or uri.query or uri.fragment:
        raise HTTPException(422, '只能预览当前文件空间内的文件')
    with httpx.Client(base_url=settings.native_cloudreve_url, timeout=45, trust_env=False,
                      headers={'Authorization': 'Bearer ' + token}, follow_redirects=False) as client:
        def call(method, path, **kwargs):
            response = client.request(method, '/api/v4/' + path, **kwargs)
            response.raise_for_status()
            payload = response.json()
            if payload.get('code') != 0:
                raise HTTPException(403, '文件不存在或没有预览权限，请刷新文件列表')
            return payload['data']

        before = call('GET', 'file/info', params={'uri': body.uri})
        extension = Path(before.get('name', '')).suffix.lower()
        if before.get('id') != body.file_id or before.get('type') != 0:
            raise HTTPException(409, '文件已经变化，请刷新后重试')
        if extension not in OFFICE_EXTENSIONS:
            raise HTTPException(415, '该格式请使用文件空间的其他预览器或下载原件')
        version = body.version or before.get('primary_entity')
        if version != before.get('primary_entity'):
            raise HTTPException(409, '当前仅支持预览最新版本，请从文件列表重新打开')
        if not isinstance(before.get('size'), int) or before['size'] < 0 or before['size'] > INPUT_LIMIT:
            raise HTTPException(413, 'Office 在线预览上限为 100 MiB，上传上限仍为 2 GiB；请下载原件查看')
        link = call('POST', 'file/url', json={'uris': [body.uri], 'entity': version,
                    'download': False, 'redirect': False, 'no_cache': True})['urls'][0]['url']
        destination = urlsplit(link)
        allowed = {urlsplit(settings.native_cloudreve_url).netloc, urlsplit(settings.native_origin).netloc}
        if destination.scheme not in {'http', 'https'} or destination.netloc not in allowed or destination.username or destination.password:
            raise HTTPException(422, '当前预览仅支持本机文件存储，未访问外部下载地址')
        root = settings.object_root().parent / 'preview-tmp'
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(root).free < INPUT_LIMIT + settings.disk_reserve_bytes:
            raise HTTPException(507, '预览临时空间不足，请稍后重试')
        with tempfile.TemporaryDirectory(prefix='office-', dir=root) as temporary:
            directory = Path(temporary)
            source = directory / 'input'
            source.mkdir(mode=0o755)
            original = source / ('document' + extension)
            size = 0
            # Signed original URL, without forwarding the caller's bearer credential.
            with httpx.stream('GET', link, timeout=45, trust_env=False, follow_redirects=False) as response:
                response.raise_for_status()
                with original.open('wb') as stream:
                    for chunk in response.iter_bytes(1024**2):
                        size += len(chunk)
                        if size > INPUT_LIMIT or size > before['size']:
                            raise HTTPException(413, '原件大小已变化或超过预览限制，请刷新后重试')
                        stream.write(chunk)
            if size != before['size']:
                raise HTTPException(409, '原件读取不完整，请刷新后重试')
            os.chmod(original, 0o444)
            after = call('GET', 'file/info', params={'uri': body.uri})
            keys = ['id', 'primary_entity', 'size', 'updated_at', 'path']
            if any(before.get(key) != after.get(key) for key in keys):
                raise HTTPException(409, '读取时文件已变化，请刷新后重试')
            content = (convert_remote(settings.preview_socket, original, extension)
                       if settings.preview_socket else convert_document(directory, extension))
            # A long conversion must not publish a file deleted/replaced while it ran.
            latest = call('GET', 'file/info', params={'uri': body.uri})
            if any(before.get(key) != latest.get(key) for key in keys):
                raise HTTPException(409, '转换时文件已变化，请刷新后重试')
            return content


def routes(app, settings):
    slots = threading.BoundedSemaphore(2)

    @app.post('/v1/native/preview')
    def preview(body: PreviewInput, x_cloudreve_token: str = Header(min_length=20, max_length=10000),
                x_akb_file_user: str = Header(min_length=1, max_length=80)):
        identity = native.verify_identity(settings, x_cloudreve_token)
        if identity['id'] != x_akb_file_user:
            raise HTTPException(409, '文件账号已变化，请重新打开预览')
        if not slots.acquire(blocking=False):
            raise HTTPException(429, '当前预览服务忙，请稍后重试')
        try:
            content = render(settings, x_cloudreve_token, body)
        except (httpx.HTTPError, ValueError, KeyError, OSError) as exc:
            raise HTTPException(503, '预览暂不可用，请重新打开或下载原件查看') from exc
        finally:
            slots.release()
        return Response(content, media_type='application/pdf', headers={
            'Cache-Control': 'no-store', 'Content-Disposition': 'inline; filename="preview.pdf"',
        })
