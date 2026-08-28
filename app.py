"""Desktop entry point.

Starts uvicorn on a free loopback port in a daemon thread, waits for
/api/health, then opens a native window pointed at it.

Nothing here needs admin rights, an installer or a background service: the
server dies with the process. If no webview runtime is usable, we fall back to
the default browser rather than failing -- the app is a local web app either
way, the window is just nicer.

    uv run python app.py            # native window
    uv run python app.py --browser  # force the browser fallback
    uv run python app.py --no-ui    # headless, e.g. to poke the API with curl
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

# Ensure `src/` is importable when running the file directly from a checkout
# that has not been `uv sync`-ed yet.
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recsys.config import get_settings  # noqa: E402
from recsys.log import get_logger, setup_logging  # noqa: E402

log = get_logger("recsys.app")

HEALTH_TIMEOUT_S = 30.0


def find_free_port(host: str) -> int:
    """Ask the OS for an unused ephemeral port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def wait_for_health(url: str, timeout: float = HEALTH_TIMEOUT_S) -> bool:
    """Poll /api/health until it answers, the server dies, or we time out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(0.15)
    return False


class ServerThread(threading.Thread):
    """uvicorn in a daemon thread, so window close ends the process."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(name="uvicorn", daemon=True)
        self.host = host
        self.port = port
        self._server = None

    def run(self) -> None:
        import uvicorn

        from recsys.api.server import create_app

        config = uvicorn.Config(
            create_app(),
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._server.run()

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.should_exit = True


def open_window(url: str, server: ServerThread) -> bool:
    """Open the pywebview window. Returns False if no webview is available."""
    try:
        import webview
    except Exception as exc:  # pywebview missing or its GUI backend unusable
        log.warning("pywebview unavailable (%s)", exc)
        return False

    try:
        window = webview.create_window(
            "Loom",
            url,
            width=1280,
            height=860,
            min_size=(720, 520),
            background_color="#0e1014",
        )
        window.events.closed += server.shutdown
        # Blocks until the window is closed.
        webview.start()
        return True
    except Exception as exc:
        # Typically a missing Edge WebView2 runtime. Not fatal -- fall back.
        log.warning("could not open a native window (%s); falling back to browser", exc)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the Loom desktop app")
    parser.add_argument("--browser", action="store_true",
                        help="open in the default browser instead of a native window")
    parser.add_argument("--no-ui", action="store_true",
                        help="serve only; do not open any UI (Ctrl+C to stop)")
    parser.add_argument("--port", type=int, default=None, help="force a specific port")
    args = parser.parse_args(argv)

    setup_logging()
    settings = get_settings()
    settings.ensure_dirs()

    host = settings.host
    port = args.port or settings.port or find_free_port(host)
    base = f"http://{host}:{port}"

    server = ServerThread(host, port)
    server.start()

    if not wait_for_health(f"{base}/api/health"):
        log.error("server did not become healthy within %.0fs", HEALTH_TIMEOUT_S)
        server.shutdown()
        return 1

    log.info("serving at %s", base)

    if args.no_ui:
        print(f"Serving at {base} -- press Ctrl+C to stop.")
        try:
            while server.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        server.shutdown()
        return 0

    opened = False
    if not args.browser:
        opened = open_window(base, server)

    if not opened:
        import webbrowser

        webbrowser.open(base)
        print(f"Opened {base} in your browser -- press Ctrl+C to stop the server.")
        try:
            while server.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass

    server.shutdown()
    server.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
