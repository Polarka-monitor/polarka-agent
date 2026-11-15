"""A web server for tests that answers what a test tells it to, and counts what it was asked."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BLOCK = "ab" * 32


def index(height=100, block=BLOCK, **change):
    i = {"ready": True, "pending_available": True, "indexed_height": height, "indexed_hash": block,
         "observed_node_height": height, "observed_node_hash": block}
    i.update(change)
    return i


class Fake:
    """answers[path] is a list of answers given one after the other (the last one stays), or a function of
    the request body. An answer is (status, body) or (status, body, headers) or a dict with more."""

    def __init__(self):
        self.answers, self.asked, self.connections = {}, [], 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                fake.connections += 1
                super().setup()

            def log_message(self, *a):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else None
                fake.asked.append((self.path, None if body is None else json.loads(body)))
                script = fake.answers.get(self.path)
                if script is None:
                    answer = (404, {"message": "no such path"})
                elif callable(script):
                    answer = script(None if body is None else json.loads(body))
                else:
                    answer = script.pop(0) if len(script) > 1 else script[0]
                if isinstance(answer, tuple):
                    answer = {"status": answer[0], "body": answer[1], "headers": answer[2] if len(answer) > 2 else {}}
                data = answer["body"]
                if not isinstance(data, (bytes, bytearray)):
                    data = json.dumps(data).encode()
                self.send_response(answer["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in answer.get("headers", {}).items():
                    self.send_header(k, v)
                if answer.get("close"):
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                step, gap = answer.get("step"), answer.get("gap", 0)
                if step:
                    try:
                        for i in range(0, len(data), step):
                            self.wfile.write(data[i:i + step])
                            self.wfile.flush()
                            time.sleep(gap)
                    except OSError:
                        pass
                else:
                    self.wfile.write(data)
                if answer.get("drop"):
                    self.close_connection = True

            do_GET = do_POST = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.handle_error = lambda *a: None       # a client that gives up in the middle of an answer is part of the tests
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
