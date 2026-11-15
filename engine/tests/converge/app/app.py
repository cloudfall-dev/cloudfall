"""The component the CI converge job deploys: 200 on ``/health``, 404 elsewhere.

Standard library only, so the release's ``uv sync`` installs nothing and the
deploy depends on no package index.
"""

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer

ADDRESS = ("127.0.0.1", 8100)


class Health(BaseHTTPRequestHandler):
    """Answer the deploy's health gate."""

    def do_GET(self) -> None:
        """Answer 200 on ``/health`` and 404 on any other path."""
        found = self.path == "/health"
        self.send_response(HTTPStatus.OK if found else HTTPStatus.NOT_FOUND)
        self.end_headers()


if __name__ == "__main__":
    HTTPServer(ADDRESS, Health).serve_forever()
