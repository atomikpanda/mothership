"""Local HTTP peer for whole-response deadline regressions."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event, Thread
from types import SimpleNamespace


@contextmanager
def response_server(*, drip=True):
    state = SimpleNamespace(sent=0, finished=Event(), requests=[])
    stop = Event()
    body = b'{"padding":"' + b'x' * 80 + b'"}'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            state.requests.append((
                self.command, self.path, self.headers.get("Authorization"),
                self.rfile.read(int(self.headers.get("Content-Length", "0"))),
            ))
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Deadline-Test", "preserved")
                self.end_headers()
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    state.sent += 1
                    if drip and stop.wait(0.02):
                        break
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                state.finished.set()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield state
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive(), "local HTTP server did not stop"
