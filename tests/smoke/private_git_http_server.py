"""Minimal Basic-auth smart-HTTP Git service for the private-Git smoke test."""

from __future__ import annotations

import base64
import hmac
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

_PORT = 8080
_USERNAME = "token-user"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def log_message(self, _format: str, *_arguments: object) -> None:
        return

    def _serve(self) -> None:
        supplied = self.headers.get("Authorization", "").encode("ascii", "ignore")
        if not hmac.compare_digest(supplied, _expected_authorization()):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="private-git"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        environment = {
            **os.environ,
            "GIT_PROJECT_ROOT": "/srv/git",
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "REQUEST_METHOD": self.command,
            "REMOTE_USER": _USERNAME,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(length),
        }
        if protocol := self.headers.get("Git-Protocol"):
            environment["HTTP_GIT_PROTOCOL"] = protocol
        completed = subprocess.run(
            ("git", "http-backend"),
            input=self.rfile.read(length),
            capture_output=True,
            check=False,
            env=environment,
        )
        if completed.returncode != 0:
            self._empty_error()
            return
        headers, separator, body = completed.stdout.partition(b"\r\n\r\n")
        if not separator:
            headers, separator, body = completed.stdout.partition(b"\n\n")
        if not separator:
            self._empty_error()
            return

        status = 200
        response_headers: list[tuple[str, str]] = []
        has_content_length = False
        for line in headers.splitlines():
            key, value = line.decode("latin-1").split(":", 1)
            if key.casefold() == "status":
                status = int(value.strip().split(" ", 1)[0])
            else:
                response_headers.append((key, value.strip()))
                has_content_length = (
                    has_content_length or key.casefold() == "content-length"
                )
        self.send_response(status)
        for key, value in response_headers:
            self.send_header(key, value)
        if not has_content_length:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _empty_error(self) -> None:
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _expected_authorization() -> bytes:
    token = Path(sys.argv[1]).read_bytes()
    return b"Basic " + base64.b64encode(_USERNAME.encode("ascii") + b":" + token)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", _PORT), _Handler).serve_forever()
