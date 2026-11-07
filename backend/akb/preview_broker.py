"""Narrow Unix-socket host service. No tokens, network URLs, paths or commands in requests."""

import argparse
import os
import shutil
import socket
import socketserver
import stat
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from akb.office_conversion import (
    INPUT_LIMIT,
    OFFICE_EXTENSIONS,
    ConversionError,
    convert_document,
)


class PreviewServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, socket_path, private_root, *, slots=2, converter=convert_document):
        self.private_root = Path(private_root)
        self.private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.private_root.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError("Converter directory must be private and owned by its operator")
        self.slots = threading.BoundedSemaphore(slots)
        self.converter = converter
        super().__init__(str(socket_path), PreviewHandler)

    def process_request(self, request, client_address):
        # Bound threads as well as conversion processes, including slow senders.
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.0 429 Too Many Requests\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        def expire():
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        deadline = threading.Timer(120, expire)
        deadline.daemon = True
        deadline.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            deadline.cancel()
            self.slots.release()


class PreviewHandler(BaseHTTPRequestHandler):
    timeout = 20

    def log_message(self, *_):
        pass  # Documents and client headers never enter host logs.

    def do_POST(self):
        try:
            self.convert()
        except ConversionError as exc:
            try:
                self.send_error(exc.status_code)
            except OSError:
                pass
        except (OSError, TimeoutError):
            # Only generic status escapes; no host paths or exception text.
            try:
                self.send_error(503)
            except OSError:
                pass

    def convert(self):
        extensions = self.headers.get_all("X-Document-Extension", [])
        lengths = self.headers.get_all("Content-Length", [])
        if self.path != "/convert":
            raise ConversionError(404, "Unknown operation")
        if len(extensions) != 1 or extensions[0] not in OFFICE_EXTENSIONS:
            raise ConversionError(415, "Unsupported suffix")
        if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal()
                or self.headers.get("Transfer-Encoding") or len(lengths[0]) > 12):
            raise ConversionError(400, "A single length is required")
        size = int(lengths[0])
        if not 0 < size <= INPUT_LIMIT:
            raise ConversionError(413, "Input too large or empty")
        if shutil.disk_usage(self.server.private_root).free < INPUT_LIMIT + 256 * 1024**2:
            raise ConversionError(507, "Insufficient temporary space")
        with tempfile.TemporaryDirectory(prefix="conversion-", dir=self.server.private_root) as directory:
            root = Path(directory)
            (root / "input").mkdir(mode=0o755)
            # systemd runs this broker with UMask=0077, while the disposable
            # converter runs as nobody. Only this read-only bind mount must be
            # traversable; the enclosing job directory remains private (0700).
            (root / "input").chmod(0o755)
            source = root / "input" / ("document" + extensions[0])
            with source.open("xb") as output:
                remaining = size
                while remaining:
                    block = self.rfile.read(min(1024**2, remaining))
                    if not block:
                        raise ConversionError(400, "Incomplete input")
                    output.write(block)
                    remaining -= len(block)
            source.chmod(0o444)
            pdf = self.server.converter(root, extensions[0])
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(pdf)))
        self.end_headers()
        self.wfile.write(pdf)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--group", type=int, required=True)
    args = parser.parse_args()
    parent = args.socket.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise SystemExit("Socket parent must be operator-owned and not writable by clients")
    # Never unlink an existing socket automatically; systemd owns its runtime directory.
    with PreviewServer(args.socket, args.private_root) as server:
        os.chown(args.socket, -1, args.group)
        args.socket.chmod(0o660)
        try:
            server.serve_forever()
        finally:
            args.socket.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
