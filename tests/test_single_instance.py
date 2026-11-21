"""二重起動の防止と、`--quit`（アンインストール時に、動いている FolderLink に終了を頼む）。

送る側は、実際と同じく別のプロセスにする（同じプロセスだと受け取る側のイベント処理が止まるため）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from folderlink.app import SingleInstance

SRC = str(Path(__file__).resolve().parents[1] / "src")
SENDER = (
    "import sys\n"
    "from PySide6.QtCore import QCoreApplication\n"
    "from folderlink.app import SingleInstance\n"
    "app = QCoreApplication([])\n"
    "ok = SingleInstance(sys.argv[1]).send(sys.argv[2].encode())\n"
    "print('sent' if ok else 'not running')\n"
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(["test"])
    yield app


def _send_from_other_process(key: str, message: str) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
               QT_QPA_PLATFORM="offscreen")
    return subprocess.Popen([sys.executable, "-c", SENDER, key, message], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait(qapp, pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("message,expect", [("quit", "quit"), ("show", "show")])
def test_message_from_other_process(qapp, tmp_path, message, expect):
    key = str(tmp_path / "inst")
    first = SingleInstance(key)
    got: list[str] = []
    first.activated.connect(lambda: got.append("show"))
    first.quitRequested.connect(lambda: got.append("quit"))
    assert first.acquire()
    try:
        proc = _send_from_other_process(key, message)
        assert _wait(qapp, lambda: got)
        assert _wait(qapp, lambda: proc.poll() is not None)
        out, err = proc.communicate()
        assert got == [expect]
        assert out.strip() == "sent", err
    finally:
        first.release()


def test_quit_when_nothing_is_running(qapp, tmp_path):
    assert SingleInstance(str(tmp_path / "nobody")).send(b"quit") is False
