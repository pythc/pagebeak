"""Credential-free parser subprocess with a CPU/time boundary, not an OS sandbox."""

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from akb.parsing import Block, Parsed, ParseError


def parse_isolated(filename: str, data: bytes | Path) -> Parsed:
    environment = {
        key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT"}
    }
    # The source-only Linux image is not an editable install. Keep the parser
    # importable without inheriting the parent's credentials or arbitrary paths.
    module_root = Path(__file__).resolve().parent.parent
    environment["PYTHONPATH"] = str(module_root)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "akb.parse_runner",
                filename,
                *([str(data.resolve())] if isinstance(data, Path) else []),
            ],
            input=None if isinstance(data, Path) else data,
            capture_output=True,
            timeout=100,
            env=environment,
            cwd=module_root,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ParseError("parse_timeout", "解析超过本地时间限制，请拆分文件或使用完整解析服务") from exc
    if result.returncode:
        raise ParseError("parse_failed", "解析进程未完成，文件已保存")
    try:
        payload = json.loads(result.stdout)
        if "error" in payload:
            raise ParseError(payload["error"], payload["message"])
        return Parsed(
            [Block(**block) for block in payload["blocks"]], payload["coverage"], payload["warning"]
        )
    except (ValueError, KeyError) as exc:
        raise ParseError("parse_failed", "解析结果格式不正确") from exc


def main():
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (90, 90))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    except (ImportError, ValueError, OSError):
        pass
    from akb.parsing import parse_document

    try:
        data = Path(sys.argv[2]) if len(sys.argv) > 2 else sys.stdin.buffer.read(8 * 1024**2 + 1)
        if not isinstance(data, Path) and len(data) > 8 * 1024**2:
            raise ParseError("file_limit", "内存输入超过解析范围，请使用文件流入库")
        payload = asdict(parse_document(sys.argv[1], data))
    except ParseError as exc:
        payload = {"error": exc.code, "message": exc.message}
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
