import os
import socket
import tempfile
import threading
from pathlib import Path

import httpx
import pytest
from akb import preview
from akb.office_conversion import INPUT_LIMIT, ConversionError
from akb.preview_broker import PreviewServer
from fastapi import HTTPException

PDF = b"%PDF-1.7\nsynthetic-isolated-preview"


@pytest.fixture
def broker():
    # Unix paths are limited to 104 bytes on macOS.
    with tempfile.TemporaryDirectory(prefix="zx-preview-", dir="/tmp") as temporary:
        root = Path(temporary)
        observations = []

        def converter(directory, extension):
            assert directory.parent == root / "private"
            assert extension == ".docx"
            source = directory / "input/document.docx"
            assert source.stat().st_mode & 0o777 == 0o444
            observations.append(source.read_bytes())
            return PDF

        with PreviewServer(root / "preview.sock", root / "private", slots=1, converter=converter) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield root, server, observations
            finally:
                server.shutdown()
                thread.join(timeout=5)


def test_unix_preview_transfers_only_bytes_and_cleans_private_files(broker):
    root, _, seen = broker
    source = root / "teacher.docx"
    source.write_bytes(b"synthetic-office-source")
    assert preview.convert_remote(root / "preview.sock", source, ".docx") == PDF
    assert seen == [b"synthetic-office-source"]
    assert not list((root / "private").iterdir())
    assert source.read_bytes() == b"synthetic-office-source"


def test_strict_service_umask_keeps_job_private_but_converter_input_readable(broker):
    root, server, _ = broker

    def converter(directory, extension):
        # A root-operated broker launches the isolated converter as nobody.
        assert directory.stat().st_mode & 0o077 == 0
        assert directory.parent.stat().st_mode & 0o077 == 0
        assert (directory / "input").stat().st_mode & 0o005 == 0o005
        assert (directory / "input/document.docx").stat().st_mode & 0o004
        return PDF

    server.converter = converter
    source = root / "service-test.docx"
    source.write_bytes(b"synthetic-office-source")
    previous = os.umask(0o077)
    try:
        assert preview.convert_remote(root / "preview.sock", source, ".docx") == PDF
    finally:
        os.umask(previous)
    assert not list((root / "private").iterdir())


@pytest.mark.parametrize("path,headers,status", [
    ("/other", {"X-Document-Extension": ".docx"}, 404),
    ("/convert", {"X-Document-Extension": "../../host-secret"}, 415),
    ("/convert", {"X-Document-Extension": ".exe"}, 415),
    ("/convert", {"X-Document-Extension": ".docx", "Content-Length": str(INPUT_LIMIT+1)}, 413),
    ("/convert", {"X-Document-Extension": ".docx", "Content-Length": "-1"}, 400),
])
def test_broker_rejects_paths_formats_and_oversize_before_conversion(broker, path, headers, status):
    root, _, seen = broker
    # Raw HTTP exercises invalid lengths which an HTTP client rejects itself.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(root / "preview.sock"))
        fields = {"Content-Length": "0", **headers}
        request = f"POST {path} HTTP/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k,v in fields.items()) + "\r\n"
        client.sendall(request.encode())
        response = client.recv(4096)
    assert response.startswith(f"HTTP/1.0 {status} ".encode())
    assert not seen and not list((root / "private").iterdir())


def test_busy_broker_has_bounded_connections(broker):
    root, server, seen = broker
    assert server.slots.acquire(blocking=False)
    try:
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(root / "preview.sock")), timeout=5) as client:
            response = client.post("http://preview/convert", headers={"X-Document-Extension": ".docx"}, content=b"doc")
        assert response.status_code == 429 and not seen
    finally:
        server.slots.release()


def test_converter_failure_cleans_job_and_hides_host_detail(broker):
    root, server, _ = broker

    def fail(*_):
        raise ConversionError(504, "/private/host/operator-secret")

    server.converter = fail
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(root / "preview.sock")), timeout=5) as client:
        response = client.post("http://preview/convert", headers={"X-Document-Extension": ".docx"}, content=b"doc")
    assert response.status_code == 504 and "operator-secret" not in response.text
    assert not list((root / "private").iterdir())


def test_api_rejects_non_pdf_from_broker(broker):
    root, server, _ = broker
    server.converter = lambda *_: b"not-a-pdf"
    source = root / "source.docx"
    source.write_bytes(b"doc")
    with pytest.raises(HTTPException) as error:
        preview.convert_remote(root / "preview.sock", source, ".docx")
    assert error.value.status_code == 422


def test_broker_rejects_incomplete_upload_and_removes_partial_original(broker):
    root, _, seen = broker
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(root / "preview.sock"))
        client.sendall(b"POST /convert HTTP/1.0\r\nX-Document-Extension: .docx\r\nContent-Length: 50\r\n\r\nshort")
        client.shutdown(socket.SHUT_WR)
        response = client.recv(4096)
    assert response.startswith(b"HTTP/1.0 400 ") and not seen
    assert not list((root / "private").iterdir())


def test_broker_refuses_a_symlink_for_its_private_directory(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match="private"):
        PreviewServer(tmp_path / "unused.sock", link)
