"""エンジンとのやり取りを、画面とは別のスレッド（QThread）で行う。

画面のスレッドは、ここのシグナルを受け取って表示を更新するだけ。エンジンの起動待ち・
REST API の呼び出し・フォルダの走査など、時間がかかることは全部ワーカー側で動く。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from folderlink.core.api_client import ApiError
from folderlink.core.pairing import PairingError
from folderlink.core.redact import redact
from folderlink.core.syncthing_manager import EngineError, StartCancelled

log = logging.getLogger(__name__)

MAX_AUTO_RESTARTS = 3


class _Worker(QObject):
    snapshot = Signal(object)
    started = Signal(str)
    failed = Signal(str)
    finished = Signal(str, bool, object)
    stopped = Signal()

    def __init__(self, service: Any, interval_ms: int, cancel: threading.Event) -> None:
        super().__init__()
        self._service = service
        self._interval_ms = interval_ms
        self._cancel = cancel
        self._timer: QTimer | None = None
        self._restarts = 0

    @Slot()
    def start(self) -> None:
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setInterval(self._interval_ms)
            self._timer.timeout.connect(self.poll)
        self._start_engine()
        self._timer.start()
        self.poll()

    def _start_engine(self) -> bool:
        try:
            device_id = self._service.start(self._cancel)
        except StartCancelled:
            return False
        except EngineError as exc:
            self.failed.emit(str(exc))
            return False
        except Exception as exc:  # 想定外でもスレッドを落とさない
            log.exception("エンジンの起動で想定外のエラー")
            self.failed.emit(f"同期エンジンを起動できませんでした（{type(exc).__name__}）。")
            return False
        self.started.emit(device_id)
        return True

    @Slot()
    def poll(self) -> None:
        if self._cancel.is_set():
            return
        try:
            if self._service.has_crashed() and self._restarts < MAX_AUTO_RESTARTS:
                self._restarts += 1
                log.warning("同期エンジンが停止したため、起動し直します（%d 回目）。", self._restarts)
                self._service.stop()
                self._start_engine()
            snap = self._service.snapshot()
        except Exception as exc:
            log.exception("状態の取得で想定外のエラー")
            self.failed.emit(f"状態を取得できませんでした（{type(exc).__name__}）。")
            return
        self.snapshot.emit(snap)

    @Slot(str, object)
    def run(self, name: str, fn: Callable[[Any], Any]) -> None:
        try:
            result = fn(self._service)
        except (PairingError, EngineError, ApiError) as exc:
            self.finished.emit(name, False, str(exc))
            return
        except Exception as exc:
            log.exception("操作 %s で想定外のエラー", name)
            self.finished.emit(name, False, redact(f"内部エラーが起きました（{type(exc).__name__}）。ログを確認してください。"))
            return
        self.finished.emit(name, True, result)
        self.poll()

    @Slot()
    def shutdown(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        try:
            self._service.stop()
        except Exception:
            log.exception("エンジンの停止で想定外のエラー")
        self.stopped.emit()
        # 画面のスレッドは wait() で待っているので、シグナル経由ではなくここで直接スレッドを終える
        QThread.currentThread().quit()


class EngineController(QObject):
    """画面側の窓口。シグナルはすべて画面のスレッドに届く。"""

    statusChanged = Signal(object)           # StatusSnapshot
    engineStarted = Signal(str)              # このPCのデバイス ID
    engineFailed = Signal(str)               # 利用者向けの説明
    actionFinished = Signal(str, bool, object)  # 名前, 成功か, 結果（失敗時は説明の文字列）

    _startRequested = Signal()
    _runRequested = Signal(str, object)
    _pollRequested = Signal()
    _shutdownRequested = Signal()

    def __init__(self, service: Any, interval_ms: int = 2000, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._cancel = threading.Event()
        self._thread = QThread()
        self._thread.setObjectName("FolderLinkEngine")
        self._worker = _Worker(service, interval_ms, self._cancel)
        self._worker.moveToThread(self._thread)
        self._worker.snapshot.connect(self.statusChanged)
        self._worker.started.connect(self._on_started)
        self._worker.failed.connect(self.engineFailed)
        self._worker.finished.connect(self.actionFinished)
        self._startRequested.connect(self._worker.start)
        self._runRequested.connect(self._worker.run)
        self._pollRequested.connect(self._worker.poll)
        self._shutdownRequested.connect(self._worker.shutdown)
        self.device_id: str | None = None
        self._shut_down = False

    @Slot(str)
    def _on_started(self, device_id: str) -> None:
        self.device_id = device_id
        self.engineStarted.emit(device_id)

    def start(self) -> None:
        """エンジンの起動を始める（すぐ戻る。結果は engineStarted / engineFailed で届く）。"""
        if not self._thread.isRunning():
            self._thread.start()
        self._startRequested.emit()

    def run(self, name: str, fn: Callable[[Any], Any]) -> None:
        """fn(service) をワーカースレッドで実行する。結果は actionFinished(name, ok, result)。"""
        self._runRequested.emit(name, fn)

    def refresh(self) -> None:
        self._pollRequested.emit()

    def shutdown(self, wait_ms: int = 30000) -> bool:
        """エンジンを止めてスレッドを終える。「終了」を選んだときだけ呼ぶ。"""
        if self._shut_down:
            return True
        self._shut_down = True
        self._cancel.set()  # 起動待ちの最中なら、すぐ中断させる
        if not self._thread.isRunning():
            try:
                self.service.stop()
            except Exception:
                log.exception("エンジンの停止で想定外のエラー")
            return True
        self._shutdownRequested.emit()
        ok = self._thread.wait(wait_ms)
        if not ok:
            log.error("エンジンのスレッドが時間内に終わりませんでした。")
        return ok
