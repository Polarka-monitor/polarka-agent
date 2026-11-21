"""One rule for every HTTP server of this package: a request that carries a body ends its connection.

Python's BaseHTTPRequestHandler does not drain a body that a handler did not read (a GET with a body, a POST whose
path is refused before it is read, a chunked body that no handler here decodes). On a kept-alive connection the
bytes left over are read as the next request: one that the web server in front never saw, with headers of the
sender's choosing (X-Forwarded-For among them). Review 8 showed it through a proxy (H1, M1). So whenever a request
carries a body, or says it does with Transfer-Encoding, the answer says Connection: close and the connection ends
after it; requests without a body keep their connection. It also takes the Python version out of the Server
header."""


class OneRequestWithBody:
    """Mix in before BaseHTTPRequestHandler: class H(OneRequestWithBody, BaseHTTPRequestHandler)."""

    sys_version = ""

    def parse_request(self):
        self._body_closes = False
        if not super().parse_request():
            return False
        length = (self.headers.get("Content-Length") or "").strip()
        if self.headers.get("Transfer-Encoding") is not None or length not in ("", "0"):
            self.close_connection = True
            self._body_closes = True
        return True

    def end_headers(self):
        if getattr(self, "_body_closes", False):
            self._body_closes = False
            self.send_header("Connection", "close")
        super().end_headers()
