"""メイン画面・トレイ・エンジンの窓口（別スレッド）。画面はオフスクリーンで、エンジンは偽物。"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest
from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from folderlink.core.config_store import Settings
from folderlink.core.pairing import PairingError
from folderlink.core.sync_status import StatusSnapshot, SyncState
from folderlink.core.syncthing_manager import EngineError, StartCancelled
from folderlink.devtools.ui_shots import sample_settings, sample_snapshots
from folderlink.ui import theme
from folderlink.ui.engine_controller import EngineController
from folderlink.ui.main_window import MainWindow
from folderlink.ui.tray import TrayController

NOW = datetime(2026, 11, 21, 3, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication(["test"])
    app.setQuitOnLastWindowClosed(False)
    yield app


def spin(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_for(pred, timeout_ms: int = 5000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if pred():
            return True
        spin(10)
    return pred()


# ---------------------------------------------------------------- ダッシュボード
def test_all_states_render_with_label_and_color(qapp):
    snaps = sample_snapshots()
    assert {s.state for s in snaps.values()} >= {SyncState.SYNCED, SyncState.SYNCING, SyncState.OFFLINE,
                                                 SyncState.PAUSED, SyncState.CONFLICT, SyncState.ERROR}
    for palette in (theme.LIGHT, theme.DARK):
        window = MainWindow(sample_settings(), None, palette)
        for snap in snaps.values():
            window.apply_snapshot(snap)
            d = window.dashboard
            assert d.state_label.text() == snap.state.label
            assert d.state_dot.color() == theme.state_color(palette, snap.state.color).upper()
            assert d.message_label.text() == snap.message
            assert not window.grab().isNull()
        window.deleteLater()
    assert theme.state_color(theme.LIGHT, "green") == theme.LIGHT.green
    assert theme.LIGHT.green != theme.LIGHT.yellow != theme.LIGHT.red


def test_synced_values(qapp):
    window = MainWindow(sample_settings(), None, theme.LIGHT)
    window.apply_snapshot(sample_snapshots()["synced"])
    d = window.dashboard
    assert d.folder_label.full_text() == r"C:\Users\taro\Desktop\共有フォルダ"
    assert d.peer_name_label.text() == "PC-B"
    assert d.peer_state_label.text() == "オンライン" and "直接" in d.peer_route_label.text()
    assert d.files_label.text() == "ファイル 248 件 ・ 1.8 GB"
    assert d.percent_label.text() == "100%" and d.progress.value() == 1000
    assert d.need_value.text() == "0 件" and d.conflict_value.text() == "0 件"
    assert d.last_sync_value.text() != "—" and d.checked_label.text() != "—"
    assert d.pause_button.text() == "同期を一時停止" and d.pause_button.isEnabled()
    assert d.banner.isHidden()


def test_unknown_values_show_dash_not_invented_numbers(qapp):
    window = MainWindow(sample_settings(), None, theme.LIGHT)
    window.apply_snapshot(StatusSnapshot(SyncState.OFFLINE, "相手のPCに接続できていません。", NOW))
    d = window.dashboard
    for lab in (d.percent_label, d.rate_value, d.need_value, d.last_sync_value, d.conflict_value,
                d.peer_state_label):
        assert lab.text() == "—", lab.text()
    assert d.files_label.text() == "ファイル —"
    assert d.progress.value() == 0


def test_relay_and_offline_and_paused_device_text(qapp):
    window = MainWindow(sample_settings(), None, theme.LIGHT)
    d = window.dashboard
    window.apply_snapshot(sample_snapshots()["relay"])
    assert "中継" in d.peer_route_label.text() and "保証" in d.peer_route_label.text()
    window.apply_snapshot(sample_snapshots()["offline"])
    assert d.peer_state_label.text() == "オフライン" and d.peer_route_label.text() == ""
    assert d.percent_label.text() == "—"
    window.apply_snapshot(sample_snapshots()["paused"])
    assert d.pause_button.text() == "同期を再開"
    assert d.files_label.text() == "ファイル —" and d.need_value.text() == "—"


def test_not_configured_shows_setup_banner_and_disables_actions(qapp):
    window = MainWindow(Settings(), None, theme.LIGHT)
    window.apply_snapshot(StatusSnapshot(SyncState.NOT_CONFIGURED, "初期設定がまだ終わっていません。", NOW))
    d = window.dashboard
    assert not d.banner.isHidden() and "初期設定" in d.banner.text.text()
    assert d.banner.button.text() == "設定を始める"
    assert not d.pause_button.isEnabled() and not d.history_button.isEnabled()
    fired = []
    d.setupRequested.connect(lambda: fired.append(1))
    d.banner.button.click()
    assert fired == [1]


def test_conflict_banner_points_to_history(qapp):
    window = MainWindow(sample_settings(), None, theme.LIGHT)
    window.apply_snapshot(sample_snapshots()["conflict"])
    d = window.dashboard
    assert not d.banner.isHidden() and d.banner.button.text() == "履歴を開く"
    assert d.conflict_value.text() == "2 件"
    window.apply_snapshot(sample_snapshots()["synced"])
    assert d.banner.isHidden()


def test_engine_error_banner_has_retry_and_clears_on_recovery(qapp):
    window = MainWindow(sample_settings(), None, theme.LIGHT)
    window._on_engine_failed("同期エンジン（syncthing.exe）が見つかりません。")
    d = window.dashboard
    assert d.state_label.text() == "エラー"
    assert not d.banner.isHidden() and d.banner.button.text() == "再試行"
    window.apply_snapshot(sample_snapshots()["error"])   # エラーのままなら帯は残る
    assert not d.banner.isHidden()
    window.apply_snapshot(sample_snapshots()["synced"])  # 回復したら消える
    assert d.banner.isHidden()


def test_window_size_limits(qapp):
    window = MainWindow(sample_settings(), None, theme.LIGHT)
    assert window.minimumWidth() == 720
    assert 720 <= window.width() <= 900


# ---------------------------------------------------------------- 閉じる・終了
def test_close_hides_to_tray_and_does_not_quit(qapp):
    window = MainWindow(sample_settings(), None, theme.LIGHT, close_to_tray=True)
    quits, hidden = [], []
    window.quitRequested.connect(lambda: quits.append(1))
    window.hiddenToTray.connect(lambda: hidden.append(1))
    window.show()
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()      # 閉じる操作は取り消され
    assert window.isHidden()           # ウィンドウは隠れるだけ
    assert quits == [] and hidden == [1]  # 終了は要求されない（=エンジンは止まらない）
    window.show_and_raise()
    assert window.isVisible()
    # 「終了」を選んだときだけ、終了が要求される
    window.request_quit()
    assert quits == [1]
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()


def test_close_without_tray_quits(qapp):
    window = MainWindow(sample_settings(), None, theme.LIGHT, close_to_tray=False)
    quits = []
    window.quitRequested.connect(lambda: quits.append(1))
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted() and quits == [1]


def test_tray_menu_and_state(qapp):
    tray = TrayController(theme.LIGHT)
    texts = [a.text() for a in tray.menu.actions() if not a.isSeparator()]
    assert texts[0] == "FolderLink を開く" and any("終了" in t for t in texts)
    opened, quits, pauses = [], [], []
    tray.openRequested.connect(lambda: opened.append(1))
    tray.quitRequested.connect(lambda: quits.append(1))
    tray.pauseToggleRequested.connect(pauses.append)
    tray.apply_snapshot(sample_snapshots()["synced"])
    assert tray.state_action.text() == "状態: 同期完了" and "同期完了" in tray.tray.toolTip()
    assert tray.pause_action.text() == "同期を一時停止" and tray.pause_action.isEnabled()
    tray.pause_action.trigger()
    tray.apply_snapshot(sample_snapshots()["paused"])
    assert tray.pause_action.text() == "同期を再開"
    tray.pause_action.trigger()
    assert pauses == [True, False]
    tray.open_action.trigger()
    tray.quit_action.trigger()
    assert opened == [1] and quits == [1]
    tray.apply_snapshot(sample_snapshots()["starting"])
    assert not tray.pause_action.isEnabled()


# ---------------------------------------------------------------- エンジンの窓口（別スレッド）
class FakeService:
    def __init__(self, start_delay: float = 0.0, fail: Exception | None = None) -> None:
        self.start_delay = start_delay
        self.fail = fail
        self.started = 0
        self.stopped = 0
        self.paused = False
        self.threads: set[int] = set()
        self.crashed = False
        self.running = False
        self.last_error: str | None = None

    def start(self, cancel: threading.Event | None = None) -> str:
        self.threads.add(threading.get_ident())
        deadline = time.monotonic() + self.start_delay
        while time.monotonic() < deadline:
            if cancel is not None and cancel.wait(0.02):
                raise StartCancelled("中断")
        if self.fail is not None:
            self.last_error = str(self.fail)  # 本物の EngineService と同じく、失敗を覚えておく
            raise self.fail
        self.last_error = None
        self.started += 1
        self.running = True
        self.crashed = False
        return "DEVICE-ID"

    def stop(self) -> None:
        self.threads.add(threading.get_ident())
        self.stopped += 1
        self.running = False

    def has_crashed(self) -> bool:
        return self.crashed

    def snapshot(self) -> StatusSnapshot:
        self.threads.add(threading.get_ident())
        if not self.running:
            if self.last_error:
                return StatusSnapshot(SyncState.ERROR, self.last_error, NOW)
            return StatusSnapshot(SyncState.STARTING, "起動中", NOW)
        state = SyncState.PAUSED if self.paused else SyncState.SYNCED
        return StatusSnapshot(state, state.label, NOW, completion=None if self.paused else 100.0)

    def set_paused(self, paused: bool) -> None:
        self.threads.add(threading.get_ident())
        self.paused = paused


def test_engine_start_does_not_block_the_ui_thread(qapp):
    """エンジンの起動に時間がかかっても、画面のスレッドは動き続ける。"""
    service = FakeService(start_delay=1.0)
    controller = EngineController(service, interval_ms=50)
    started, ticks = [], []
    controller.engineStarted.connect(started.append)
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    t0 = time.monotonic()
    controller.start()
    assert time.monotonic() - t0 < 0.3  # start() はすぐ戻る
    assert wait_for(lambda: started == ["DEVICE-ID"], 5000)
    timer.stop()
    during = [t for t in ticks if t - t0 < 1.0]
    assert len(during) >= 20, f"起動待ちの間に画面のタイマーが {len(during)} 回しか動いていない"
    gaps = [b - a for a, b in zip(during, during[1:])]
    assert max(gaps) < 0.3, "画面のスレッドが止まった時間がある"
    # エンジンの処理は画面のスレッドでは動いていない
    assert threading.get_ident() not in service.threads
    assert controller.device_id == "DEVICE-ID"
    assert controller.shutdown()
    assert service.stopped == 1


def test_status_updates_and_actions_flow_through_signals(qapp):
    service = FakeService()
    controller = EngineController(service, interval_ms=30)
    window = MainWindow(sample_settings(), controller, theme.LIGHT)
    done = []
    controller.actionFinished.connect(lambda name, ok, result: done.append((name, ok, result)))
    controller.start()
    assert wait_for(lambda: window.dashboard.state_label.text() == "同期完了")
    # 一時停止ボタン → ワーカースレッドで実行 → 表示が変わる
    window.dashboard.pause_button.click()
    assert not window.dashboard.pause_button.isEnabled()  # 結果が出るまで連打できない
    assert wait_for(lambda: window.dashboard.state_label.text() == "一時停止")
    assert service.paused is True and ("pause", True, None) in done
    assert window.dashboard.pause_button.text() == "同期を再開" and window.dashboard.pause_button.isEnabled()
    window.dashboard.pause_button.click()
    assert wait_for(lambda: window.dashboard.state_label.text() == "同期完了")
    assert threading.get_ident() not in service.threads
    assert controller.shutdown()


def test_action_errors_are_reported_not_raised(qapp):
    service = FakeService()
    controller = EngineController(service, interval_ms=1000)
    done = []
    controller.actionFinished.connect(lambda name, ok, result: done.append((name, ok, result)))
    controller.start()

    def boom(_service):
        raise PairingError("デバイス ID が正しくありません。")

    def crash(_service):
        raise RuntimeError("secret-internal-detail")

    controller.run("a", boom)
    controller.run("b", crash)
    controller.run("c", lambda s: 42)
    assert wait_for(lambda: len(done) == 3)
    assert done[0] == ("a", False, "デバイス ID が正しくありません。")
    assert done[1][0] == "b" and done[1][1] is False and "secret-internal-detail" not in done[1][2]
    assert done[2] == ("c", True, 42)
    assert controller.shutdown()


def test_engine_failure_is_shown_with_retry(qapp):
    service = FakeService(fail=EngineError("同期エンジン（syncthing.exe）が見つかりません。"))
    controller = EngineController(service, interval_ms=1000)
    window = MainWindow(sample_settings(), controller, theme.LIGHT)
    controller.start()
    assert wait_for(lambda: not window.dashboard.banner.isHidden())
    assert "見つかりません" in window.dashboard.banner.text.text()
    assert window.dashboard.state_label.text() == "エラー"
    # 再試行で起動し直す
    service.fail = None
    window.dashboard.banner.button.click()
    assert wait_for(lambda: service.started == 1)
    assert controller.shutdown()


def test_shutdown_interrupts_a_slow_start(qapp):
    service = FakeService(start_delay=30.0)
    controller = EngineController(service, interval_ms=1000)
    controller.start()
    spin(150)
    t0 = time.monotonic()
    assert controller.shutdown(wait_ms=5000)
    assert time.monotonic() - t0 < 3.0  # 30 秒待たずに中断できる
    assert service.started == 0 and service.stopped == 1
    assert controller.shutdown()  # 2回呼んでも問題ない
    assert service.stopped == 1


def test_crashed_engine_is_restarted_a_limited_number_of_times(qapp):
    service = FakeService()
    controller = EngineController(service, interval_ms=20)
    controller.start()
    assert wait_for(lambda: service.started == 1)
    service.crashed = True
    assert wait_for(lambda: service.started == 2)  # 自動で起動し直す
    assert controller.shutdown()


def test_shutdown_without_start_stops_service_once(qapp):
    service = FakeService()
    controller = EngineController(service)
    assert controller.shutdown()
    assert service.stopped == 1
    assert not controller._thread.isRunning()
    assert isinstance(controller._thread, QThread)
