"""Test helpers. Everything here runs offline against the FAKE server."""
import os
import sys
import threading

SERVING = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(SERVING)
if SERVING not in sys.path:
    sys.path.insert(0, SERVING)

import fake_openai_server  # noqa: E402


class FakeServer:
    """Context manager: the fake OpenAI-compatible server on a free local port."""

    def __init__(self, **kw):
        self.srv = fake_openai_server.make_server(port=0, **kw)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()
