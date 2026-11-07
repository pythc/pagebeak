"""Credential-free Office conversion; also usable by the stdlib-only host broker."""
import os
import subprocess
from pathlib import Path
from uuid import uuid4

OFFICE_EXTENSIONS = {'.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.odt', '.ods', '.odp', '.rtf'}
# Reuse the installed, pinned image's LibreOffice and CJK fonts, with no file-service volume.
OFFICE_IMAGE = 'cloudreve/cloudreve:4.19.1@sha256:5c6c5baf7fa5bc6e1bb9a7509c09dde00e80b618d180244eb1d5f7e36b358ea2'
INPUT_LIMIT = 100 * 1024**2
OUTPUT_LIMIT = 64 * 1024**2


class ConversionError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def convert_document(directory: Path, extension: str) -> bytes:
    source, output, profile = directory / 'input', directory / 'output', directory / 'profile'
    output.mkdir(mode=0o777)
    profile.mkdir(mode=0o777)
    # The mount parent is private to the API; only these disposable subdirectories are shared.
    os.chmod(output, 0o777)
    os.chmod(profile, 0o777)
    (profile / 'user').mkdir(mode=0o777)
    os.chmod(profile / 'user', 0o777)
    policy = profile / 'user/registrymodifications.xcu'
    policy.write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop><prop oor:name="SecureURL" oor:op="fuse"><value/></prop></item>
</oor:items>''')
    os.chmod(policy, 0o666)
    container = 'akb-preview-' + uuid4().hex
    user = f'{os.getuid() or 65534}:{os.getgid() if os.getuid() else 65534}'
    command = [
        'docker', 'run', '--rm', '--pull=never', '--name', container,
        '--network=none', '--read-only', '--user=' + user, '--cap-drop=ALL',
        '--security-opt=no-new-privileges', '--pids-limit=128', '--memory=768m', '--cpus=1',
        '--ulimit', 'fsize=67108864:67108864', '--tmpfs', '/tmp:rw,nosuid,noexec,size=384m',
        '--env', 'HOME=/tmp', '--env', 'LANG=C.UTF-8',
        '--mount', f'type=bind,source={source},target=/input,readonly',
        '--mount', f'type=bind,source={output},target=/output',
        '--mount', f'type=bind,source={profile},target=/profile',
        '--entrypoint', '/usr/bin/libreoffice', OFFICE_IMAGE,
        '-env:UserInstallation=file:///profile', '--headless', '--nologo', '--nodefault',
        '--norestore', '--nolockcheck', '--convert-to', 'pdf', '--outdir', '/output',
        '/input/document' + extension,
    ]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ConversionError(504, '预览转换超时，请下载原件查看，或拆分文件后重试') from exc
    except OSError as exc:
        raise ConversionError(503, '本地预览服务尚未就绪，请下载原件查看') from exc
    finally:
        # Killing the docker CLI alone would leave a conversion container running after timeout.
        try:
            subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    pdf = output / 'document.pdf'
    if result.returncode or not pdf.is_file() or pdf.is_symlink() or not 0 < pdf.stat().st_size <= OUTPUT_LIMIT:
        raise ConversionError(422, '无法生成预览；文件可能损坏、加密或过于复杂，请下载原件查看')
    content = pdf.read_bytes()
    if not content.startswith(b'%PDF-'):
        raise ConversionError(422, '转换没有生成有效 PDF，请下载原件查看')
    return content

