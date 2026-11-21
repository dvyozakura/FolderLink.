"""テスト用: 偽の Syncthing（127.0.0.1 の空きポートで動く小さな HTTP サーバー）と偽のプロセス。

「8384 に利用者の別の Syncthing がいる」状況を、実物に触れずに再現するために使う。
受け取った要求をすべて記録するので、「相手に何を送ったか／送らなかったか」を確かめられる。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

OWN_ID = "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"
OTHER_ID = "ZZZZZZZ-YYYYYYY-XXXXXXX-WWWWWWW-VVVVVVV-UUUUUUU-TTTTTTT-SSSSSSS"

SAMPLE_CONFIG_XML = """<configuration version="51">
    <gui enabled="true" tls="false">
        <address>127.0.0.1:8384</address>
        <apikey>generated-by-syncthing</apikey>
    </gui>
    <options>
        <listenAddress>default</listenAddress>
        <globalAnnounceEnabled>true</globalAnnounceEnabled>
        <localAnnounceEnabled>true</localAnnounceEnabled>
        <relaysEnabled>true</relaysEnabled>
        <natEnabled>true</natEnabled>
        <urAccepted>0</urAccepted>
        <autoUpgradeIntervalH>12</autoUpgradeIntervalH>
        <startBrowser>true</startBrowser>
    </options>
</configuration>
"""


class FakeEngine:
    """偽の Syncthing。

    my_id        : /rest/system/status が返すデバイス ID
    required_key : このキーでなければ 403。None なら**どんなキーでも受け付ける**（最悪の相手を想定）
    """

    def __init__(self, my_id: str = OWN_ID, required_key: str | None = None) -> None:
        self.my_id = my_id
        self.required_key = required_key
        self.requests: list[tuple[str, str]] = []
        self.on_shutdown = None  # shutdown を受けたときに呼ぶ関数
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _serve(self, method: str) -> None:
                path = self.path.split("?")[0]
                engine.requests.append((method, path))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                if engine.required_key is not None and self.headers.get("X-API-Key") != engine.required_key:
                    self._send(403, "Forbidden")
                    return
                if method == "GET" and path == "/rest/system/status":
                    self._send(200, json.dumps({"myID": engine.my_id, "uptime": 1}))
                elif method == "GET" and path == "/rest/system/ping":
                    self._send(200, json.dumps({"ping": "pong"}))
                elif method == "GET" and path == "/rest/system/version":
                    self._send(200, json.dumps({"version": "v2.1.7"}))
                elif method == "POST" and path == "/rest/system/shutdown":
                    self._send(200, json.dumps({"ok": "shutting down"}))
                    if engine.on_shutdown:
                        engine.on_shutdown()
                else:
                    self._send(200, "{}")

            def _send(self, status: int, body: str) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._serve("GET")

            def do_POST(self) -> None:
                self._serve("POST")

            def do_PUT(self) -> None:
                self._serve("PUT")

            def do_PATCH(self) -> None:
                self._serve("PATCH")

            def do_DELETE(self) -> None:
                self._serve("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def writes(self) -> list[tuple[str, str]]:
        """状態の問い合わせ以外に受け取った要求（設定変更・停止など）。"""
        return [r for r in self.requests if r[0] != "GET"]

    def __enter__(self) -> "FakeEngine":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class FakeProc:
    """subprocess.Popen の代わり。本物のプロセスは起動しない。"""

    _next_pid = 900001

    def __init__(self, args: list[str] | None = None, **kwargs: Any) -> None:
        self.args = args or []
        self.kwargs = kwargs
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.pid = FakeProc._next_pid
        FakeProc._next_pid += 1

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        # 本物の Popen.wait と同じく、終わるか時間切れまで待つ（偽サーバーのスレッドが exit() を呼ぶのを待てるように）
        import subprocess
        import time

        deadline = time.monotonic() + (timeout or 0)
        while self.returncode is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout or 0)
            time.sleep(0.01)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 1

    def kill(self) -> None:
        self.killed = True
        self.returncode = 1

    def exit(self, code: int = 0) -> None:
        self.returncode = code


class FakeRunner:
    """subprocess.run の代わり。`generate` と `device-id` だけを真似る。"""

    def __init__(self, device_id: str = OWN_ID, device_id_output: str | None = None) -> None:
        self.device_id = device_id
        self.device_id_output = device_id_output
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> Any:
        self.calls.append({"args": list(args), **kwargs})
        home = next((Path(a.split("=", 1)[1]) for a in args if a.startswith("--home=")), None)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        result = Result()
        if "generate" in args and home is not None:
            home.mkdir(parents=True, exist_ok=True)
            (home / "config.xml").write_text(SAMPLE_CONFIG_XML, encoding="utf-8")
            (home / "cert.pem").write_text("fake cert", encoding="utf-8")
            (home / "key.pem").write_text("fake key", encoding="utf-8")
        elif "device-id" in args:
            result.stdout = (self.device_id_output if self.device_id_output is not None else self.device_id) + "\n"
        return result

    def count(self, sub: str) -> int:
        return sum(1 for c in self.calls if sub in c["args"])
