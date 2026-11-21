"""初期設定ウィザード・接続デバイス・設定・ログの各画面。画面はオフスクリーン、エンジンは偽物。"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from fake_client import MY_ID
from folderlink.app import install_pages
from folderlink.core import autostart as au
from folderlink.core import ignore_presets
from folderlink.core.autostart import AutoStart, MemoryRegistry
from folderlink.core.config_store import ConfigStore
from folderlink.core.engine_service import EngineService, PreviewInfo
from folderlink.core.pairing import PendingDevice, derive_folder_id, normalize_device_id
from folderlink.core.redact import register_secret
from folderlink.core.sync_status import SyncState
from folderlink.ui import theme
from folderlink.ui.main_window import MainWindow
from folderlink.ui.setup_wizard import STEP_TITLES, format_id_two_lines
from test_engine_service import FakeManager

PEER = "P56IOI7-MZJNU2Y-IQGDREY-DM2MGTI-MGL3BXN-PQ6W5BM-TBBZ4TJ-XZWICQ2"
OTHER = normalize_device_id("B" * 52)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication(["test"])
    app.setQuitOnLastWindowClosed(False)
    yield app


class SyncController(QObject):
    """EngineController の代わり。操作をその場で実行して、結果をシグナルで返す。"""

    statusChanged = Signal(object)
    engineStarted = Signal(str)
    engineFailed = Signal(str)
    actionFinished = Signal(str, bool, object)

    def __init__(self, service) -> None:
        super().__init__()
        self.service = service
        self.device_id = None
        self.log: list[str] = []

    def boot(self) -> None:
        self.device_id = self.service.start()
        self.engineStarted.emit(self.device_id)

    def run(self, name, fn) -> None:
        self.log.append(name)
        try:
            result = fn(self.service)
        except Exception as exc:  # 本物と同じく、失敗は文字列で返す
            self.actionFinished.emit(name, False, str(exc))
            return
        self.actionFinished.emit(name, True, result)

    def start(self) -> None:
        self.boot()

    def refresh(self) -> None:
        self.statusChanged.emit(self.service.snapshot())


class Rig:
    def __init__(self, tmp_path, frozen: bool = True, confirm_answer: bool = True) -> None:
        FakeManager.instances.clear()
        FakeManager.fail = None
        self.store = ConfigStore(tmp_path / "data")
        self.service = EngineService(self.store, manager_factory=FakeManager)
        self.controller = SyncController(self.service)
        self.registry = MemoryRegistry()
        self.autostart = AutoStart(self.registry, r"C:\Program Files\FolderLink\FolderLink.exe", frozen=frozen)
        self.confirms: list[str] = []
        self.confirm_answer = confirm_answer
        self.chosen_folder = ""
        self.window = MainWindow(self.service.settings, self.controller, theme.LIGHT)
        self.pages = install_pages(
            self.window, self.controller, self.service, self.store, theme.LIGHT, autostart=self.autostart,
            confirm=self._confirm, choose_folder=lambda _parent, _start: self.chosen_folder)
        self.controller.boot()
        self.folder = tmp_path / "共有フォルダ"
        self.folder.mkdir()

    def _confirm(self, _parent, title, _text) -> bool:
        self.confirms.append(title)
        return self.confirm_answer

    @property
    def client(self):
        return FakeManager.instances[0].client_obj

    @property
    def wizard(self):
        return self.pages["setup"]

    def connect_peer(self, connected: bool = True) -> None:
        self.client.conns["connections"][PEER] = {"connected": connected, "paused": False,
                                                  "address": "203.0.113.5:22000", "type": "tcp-client",
                                                  "clientVersion": "v2.1.7"}


@pytest.fixture
def rig(qapp, tmp_path):
    return Rig(tmp_path)


# ---------------------------------------------------------------- ウィザード
def test_wizard_has_six_steps_in_spec_order(rig):
    assert len(STEP_TITLES) == 6 and len(rig.wizard.step_labels) == 6
    assert [lab.text()[0] for lab in rig.wizard.step_labels] == list("①②③④⑤⑥")
    assert rig.wizard.stack.count() == 6
    assert format_id_two_lines(MY_ID).count("\n") == 1
    assert format_id_two_lines(MY_ID).replace("\n", "") == MY_ID


def test_wizard_full_flow(rig):
    w = rig.wizard
    rig.window.dashboard.banner.button.click()  # 「設定を始める」
    assert rig.window.current_page() == "setup" and w.current_step() == 0
    assert not w.next_button.isEnabled()  # フォルダ未入力では進めない

    # ① 存在しないフォルダ → 理由を表示して進まない
    w.folder_edit.setText(str(rig.folder / "missing"))
    w.next_button.click()
    assert w.current_step() == 0 and "見つかりません" in w.message.text()
    # 「参照…」で選ぶ
    rig.chosen_folder = str(rig.folder)
    w.browse_button.click()
    w.preset_checks["os_junk"].setChecked(True)
    w.next_button.click()
    assert w.current_step() == 1 and w.message.text() == ""

    # ② 自分の ID の表示とコピー
    assert w.my_id_label.text().replace("\n", "") == MY_ID
    w.copy_button.click()
    assert QGuiApplication.clipboard().text() == MY_ID and w.copy_status.text() == "コピーしました"
    w.next_button.click()
    assert w.current_step() == 2

    # ③ 入力の検証
    assert not w.next_button.isEnabled()
    w.peer_edit.setPlainText("ABC")
    assert "長さ" in w.peer_status.text() and not w.next_button.isEnabled()
    w.peer_edit.setPlainText(PEER[:-1] + "A")
    assert "正しくありません" in w.peer_status.text() and not w.next_button.isEnabled()
    w.peer_edit.setPlainText(MY_ID)
    assert "このPC自身" in w.peer_status.text() and not w.next_button.isEnabled()
    QGuiApplication.clipboard().setText("  " + PEER.lower() + "\n")
    w.paste_button.click()  # 貼り付け（小文字・前後の空白つきでも通る）
    assert w.peer_id == PEER and w.next_button.isEnabled()
    w.peer_name_edit.setText("PC-B")
    assert rig.client.called("add_device") == []  # まだ登録していない
    w.next_button.click()

    # ④ 登録と接続待ち
    assert w.current_step() == 3
    assert rig.client.devices[PEER]["name"] == "PC-B"
    assert rig.service.settings.peer_device_id == PEER
    assert w.approve_local.text() == "登録しました"
    w._poll_tick()
    assert "待っています" in w.approve_remote.text()
    assert w.next_button.text() == "接続を待たずに次へ"
    rig.connect_peer()
    w._poll_tick()
    assert "接続しました" in w.approve_remote.text() and "直接" in w.approve_remote.text()
    assert w.next_button.text() == "次へ"
    w.next_button.click()

    # ⑤ プレビュー: フォルダは「送信のみ」で登録される（このPCのファイルは書き換わらない）
    assert w.current_step() == 4
    fid = derive_folder_id(MY_ID, PEER)
    assert rig.client.folders[fid]["type"] == "sendonly"
    assert rig.service.settings.setup_completed is False
    assert "(?d)Thumbs.db" in ignore_presets.read_stignore(rig.folder)
    # 相手の情報がまだ無いときは「—」
    w.apply_preview(PreviewInfo(local_files=3, local_bytes=1000, peer_connected=True, remote_ready=False))
    assert w.pv_local.text() == "3 件 ・ 1000 B"
    assert w.pv_receive.text() == w.pv_send.text() == w.pv_differ.text() == "—"
    assert "相手のPCがこの画面まで進むと" in w.preview_banner.text.text()
    # 両方にファイルがあるときは警告
    w.apply_preview(PreviewInfo(local_files=3, local_bytes=1000, global_files=5, peer_connected=True,
                                remote_ready=True, receive_items=2, receive_bytes=50, send_items=1,
                                send_bytes=10, differing=1))
    assert "両方のフォルダに" in w.preview_banner.text.text()
    assert w.preview_banner.property("kind") == "warn"
    assert w.pv_receive.text() == "2 件 ・ 50 B" and w.pv_differ.text() == "1 件"
    w.next_button.click()

    # ⑥ 確認のチェックを入れるまで開始できない
    assert w.current_step() == 5 and w.next_button.text() == "同期を開始"
    assert str(rig.folder) in w.summary_label.text() and "PC-B" in w.summary_label.text()
    assert not w.next_button.isEnabled()
    w.confirm_check.setChecked(True)
    finished = []
    w.finished.connect(lambda: finished.append(1))
    w.next_button.click()
    assert rig.client.folders[fid]["type"] == "sendreceive"
    assert rig.service.settings.setup_completed is True
    assert "開始しました" in w.start_status.text() and w.next_button.text() == "ダッシュボードへ"
    assert not w.back_button.isEnabled()
    w.next_button.click()
    assert finished == [1] and rig.window.current_page() == "dashboard"
    # ダッシュボードが設定済みの表示になる
    assert rig.window.dashboard.folder_label.full_text() == str(rig.folder)
    assert rig.window.dashboard.peer_name_label.text() == "PC-B"
    assert fid in rig.client.folders  # 終了後もフォルダは登録されたまま


def test_leaving_wizard_midway_removes_preview_folder_but_keeps_files(rig):
    (rig.folder / "keep.txt").write_text("data", encoding="utf-8")
    w = rig.wizard
    rig.window.show_page("setup")
    w.folder_edit.setText(str(rig.folder))
    w.next_button.click()
    w.next_button.click()
    w.peer_edit.setPlainText(PEER)
    w.next_button.click()
    w.next_button.click()  # ⑤ へ（接続を待たずに）
    fid = derive_folder_id(MY_ID, PEER)
    assert w.current_step() == 4 and fid in rig.client.folders
    w.cancel_button.click()  # やめる
    assert rig.window.current_page() == "dashboard"
    assert fid not in rig.client.folders          # プレビュー用の登録は外れる
    assert rig.service.settings.folder_id is None and rig.service.settings.setup_completed is False
    assert rig.service.settings.peer_device_id == PEER  # 相手の登録は残る
    assert (rig.folder / "keep.txt").read_text(encoding="utf-8") == "data"  # ファイルはそのまま
    # 「戻る」（見出しのボタン）で離れても同じ
    rig.window.show_page("setup")
    w.restart(rig.service.settings)
    assert w.peer_edit.toPlainText() == PEER  # 相手の ID は入力済みになっている


def test_wizard_detects_unity_project_and_suggests_preset(rig):
    for d in ("Assets", "ProjectSettings", "Library"):
        (rig.folder / d).mkdir()
    w = rig.wizard
    assert not w.preset_checks["unity"].isChecked()
    w.folder_edit.setText(str(rig.folder))
    assert w.preset_checks["unity"].isChecked() and "Unity プロジェクトが見つかりました" in w.unity_hint.text()


def test_wizard_shows_pending_device_and_can_use_it(rig):
    w = rig.wizard
    w._show_step(2)
    rig.client.pending_dev = {PEER: {"time": "2026-11-21T03:00:00Z", "name": "PC-B", "address": "1.2.3.4:22000"}}
    w._poll_tick()
    assert not w.pending_card.isHidden() and PEER in w.pending_label.text()
    w.pending_use_button.click()
    assert w.peer_id == PEER


def test_wizard_waits_for_engine_before_showing_id(qapp, tmp_path):
    FakeManager.instances.clear()
    service = EngineService(ConfigStore(tmp_path / "d"), manager_factory=FakeManager)
    controller = SyncController(service)
    window = MainWindow(service.settings, controller, theme.LIGHT)
    pages = install_pages(window, controller, service, service.store, theme.LIGHT,
                          autostart=AutoStart(MemoryRegistry(), "x.exe", frozen=False),
                          confirm=lambda *_a: False, choose_folder=lambda *_a: "")
    w = pages["setup"]
    assert "準備中" in w.my_id_label.text() and not w.copy_button.isEnabled()
    controller.boot()
    assert w.my_id_label.text().replace("\n", "") == MY_ID and w.copy_button.isEnabled()


# ---------------------------------------------------------------- フォルダ・相手の変更
def configure(rig: Rig) -> str:
    rig.service.register_peer(PEER, "PC-B")
    fid = rig.service.start_sync(str(rig.folder))
    rig.window.apply_settings(rig.service.settings)
    rig.pages["devices"].apply_settings(rig.service.settings)
    rig.pages["settings"].apply_settings(rig.service.settings)
    return fid


def test_change_folder_needs_confirmation_and_keeps_files(rig):
    fid = configure(rig)
    (rig.folder / "keep.txt").write_text("data", encoding="utf-8")
    rig.confirm_answer = False
    rig.window.dashboard.change_button.click()
    assert rig.confirms == ["同期フォルダの変更"] and fid in rig.client.folders  # 断ったら何も変わらない
    rig.confirm_answer = True
    rig.window.dashboard.change_button.click()
    assert fid not in rig.client.folders
    assert rig.window.current_page() == "setup" and rig.wizard.current_step() == 0
    assert rig.service.settings.peer_device_id == PEER
    assert (rig.folder / "keep.txt").exists()


def test_device_panel_shows_ids_and_change_peer(rig):
    fid = configure(rig)
    d = rig.pages["devices"]
    rig.window.show_page("devices")
    assert d.my_id_label.text().replace("\n", "") == MY_ID
    assert d.values["id"].text().replace("\n", "") == PEER and d.values["name"].text() == "PC-B"
    d.copy_button.click()
    assert QGuiApplication.clipboard().text() == MY_ID
    rig.confirm_answer = False
    d.change_button.click()
    assert PEER in rig.client.devices and fid in rig.client.folders
    rig.confirm_answer = True
    d.change_button.click()
    assert PEER not in rig.client.devices and fid not in rig.client.folders
    assert rig.service.settings.peer_device_id is None
    assert rig.window.current_page() == "setup"


def test_device_panel_pending_accept_and_reject(rig):
    d = rig.pages["devices"]
    rig.client.pending_dev = {OTHER: {"time": "t", "name": "知らないPC", "address": "198.51.100.7:22000"}}
    rig.window.show_page("devices")
    assert not d.pending_card.isHidden() and OTHER in d.pending_text.text()
    d.reject_button.click()
    assert rig.client.called("dismiss_pending_device") == [(OTHER,)]
    assert d.pending_card.isHidden() and OTHER not in rig.client.devices
    rig.client.pending_dev = {PEER: {"time": "t", "name": "PC-B", "address": "x"}}
    d.refresh()
    assert d.accept_button.isEnabled()
    d.accept_button.click()
    assert PEER in rig.client.devices and rig.service.settings.peer_device_id == PEER
    # 相手が登録済みのときは、別の PC を承認できない（相手は1台）
    rig.client.pending_dev = {OTHER: {"time": "t", "name": "", "address": ""}}
    d.refresh()
    assert not d.pending_card.isHidden() and not d.accept_button.isEnabled()


def test_device_panel_snapshot_states(rig):
    from folderlink.devtools.ui_shots import sample_snapshots

    configure(rig)
    d = rig.pages["devices"]
    rig.window.apply_snapshot(sample_snapshots()["relay"])
    assert d.values["state"].text() == "オンライン" and d.values["route"].text() == "中継サーバー経由"
    assert "保証" in d.route_note.text() or "限りません" in d.route_note.text()
    rig.window.apply_snapshot(sample_snapshots()["offline"])
    assert d.values["state"].text() == "オフライン" and d.values["route"].text() == "—"


# ---------------------------------------------------------------- 設定
def test_settings_autostart_uses_injected_registry_only(rig):
    s = rig.pages["settings"]
    rig.window.show_page("settings")
    assert not s.autostart_check.isChecked() and s.autostart_check.isEnabled()
    s.autostart_check.setChecked(True)
    assert rig.registry.values == {
        (au.RUN_KEY, "FolderLink"): r'"C:\Program Files\FolderLink\FolderLink.exe" --minimized'}
    s.autostart_check.setChecked(False)
    assert rig.registry.values == {}
    assert s.message.text() == ""


def test_settings_autostart_is_refused_in_development_run(qapp, tmp_path):
    rig = Rig(tmp_path, frozen=False)
    s = rig.pages["settings"]
    assert not s.autostart_check.isEnabled() and "開発中" in s.autostart_note.text()
    s._on_autostart(True)  # 無理に呼んでも登録されない
    assert rig.registry.values == {} and "開発中" in s.message.text()
    assert not s.autostart_check.isChecked()


def test_settings_theme_and_minimized_are_saved(rig):
    s = rig.pages["settings"]
    s.theme_combo.setCurrentIndex(s.theme_combo.findData("dark"))
    s.minimized_check.setChecked(True)
    saved = ConfigStore(rig.store.base_dir).load_settings()
    assert saved.theme == "dark" and saved.start_minimized is True
    theme.apply_theme(QApplication.instance(), "light")  # 他のテストのために戻す


def test_settings_apply_ignores_and_days(rig):
    fid = configure(rig)
    s = rig.pages["settings"]
    s.preset_checks["unity"].setChecked(True)
    s.custom_edit.setPlainText("*.blend1\n\n#include x\n  /作業中  ")
    s.apply_ignores_button.click()
    lines = rig.client.ignores[fid]
    assert "*.blend1" in lines and "/作業中" in lines and "/Library" in lines
    assert not any("#include" in line for line in lines)
    assert "適用しました" in s.ignores_status.text()
    assert rig.service.settings.custom_ignores == ["*.blend1", "/作業中"]
    s.days_spin.setValue(90)
    s.apply_days_button.click()
    assert rig.client.folders[fid]["versioning"]["params"]["maxAge"] == str(90 * 86400)
    assert rig.client.folders[fid]["versioning"]["type"] == "staggered"
    assert ConfigStore(rig.store.base_dir).load_settings().versioning_days == 90


def test_settings_reset_needs_confirmation_and_keeps_files(rig):
    fid = configure(rig)
    (rig.folder / "keep.txt").write_text("data", encoding="utf-8")
    s = rig.pages["settings"]
    rig.confirm_answer = False
    s.reset_button.click()
    assert fid in rig.client.folders
    rig.confirm_answer = True
    s.reset_button.click()
    assert fid not in rig.client.folders and PEER not in rig.client.devices
    assert (rig.folder / "keep.txt").read_text(encoding="utf-8") == "data"
    assert rig.window.current_page() == "dashboard"
    assert not rig.window.dashboard.banner.isHidden()  # 「初期設定がまだ」の帯が出る


def test_settings_about_and_notices(rig):
    s = rig.pages["settings"]
    about = s.about_label.text()
    assert "Syncthing v2.1.7" in about and "Mozilla Public License 2.0" in about
    assert "github.com/syncthing/syncthing" in about and "公式ソフトウェアではありません" in about
    texts = " ".join(lab.text() for lab in s.findChildren(type(s.about_label)))
    assert "Unity Version Control" in texts and "Git" in texts
    from folderlink.ui.main_window import BACKUP_NOTICE

    assert "バックアップではありません" in BACKUP_NOTICE
    assert rig.window.dashboard.notice_label.text() == BACKUP_NOTICE


# ---------------------------------------------------------------- ログ
def test_log_view_redacts_secrets(rig):
    secret = "log-view-secret-key-0123456789abcdef"
    register_secret(secret)
    view = rig.pages["log"]
    view.set_app_log(f"line 1\nX-API-Key: {secret}\nline 3")
    view.set_engine_log(f"apikey={secret}")
    assert secret not in view.app_text.toPlainText() and "***" in view.app_text.toPlainText()
    assert secret not in view.engine_text.toPlainText()
    view.set_app_log("")
    assert "まだログがありません" in view.app_text.toPlainText()
    rig.window.show_page("log")  # 表示するとエンジンのログも取りに行く
    assert view.engine_text.toPlainText() == "engine log"
    assert "log:engine" in rig.controller.log
    view.copy_button.click()
    assert QGuiApplication.clipboard().text() == view.tabs.currentWidget().toPlainText()


def test_not_configured_state_while_previewing(rig):
    rig.service.register_peer(PEER)
    rig.service.prepare_preview(str(rig.folder))
    assert rig.service.snapshot().state is SyncState.NOT_CONFIGURED


# ---------------------------------------------------------------- 履歴・競合
CONFLICT_COPY = "doc.sync-conflict-20261121-123456-ABCDEFG.txt"


def _configure_history(rig):
    s = rig.service.settings
    s.setup_completed, s.folder_path, s.folder_id = True, str(rig.folder), "folderlink-hist"
    s.peer_device_id = PEER
    (rig.folder / "doc.txt").write_text("いまの版", encoding="utf-8")
    (rig.folder / CONFLICT_COPY).write_text("競合コピー", encoding="utf-8")
    rig.client.versions["folderlink-hist"] = {
        "gone.bin": [{"versionTime": "2026-11-21T13:00:07+09:00", "modTime": "2026-11-21T12:58:58+09:00",
                      "size": 2048}],
        "doc.txt": [{"versionTime": "2026-11-21T12:59:01+09:00", "modTime": "2026-11-21T12:58:58+09:00",
                     "size": 2}],
    }
    return rig.pages["history"]


def test_history_page_lists_conflicts_and_versions(rig):
    view = _configure_history(rig)
    rig.window.dashboard.historyRequested.emit()
    assert rig.window.current_page() == "history"
    assert "history:load" in rig.controller.log
    assert view.conflict_table.rowCount() == 1
    assert view.conflict_table.item(0, 0).text() == "doc.txt"
    assert view.tabs.tabText(0) == "競合（1）" and not view.banner.isHidden()
    assert view.version_table.rowCount() == 2
    assert view.version_table.item(0, 0).text() == "gone.bin"
    assert view.version_table.item(0, 4).text() == "削除済み"
    assert view.version_table.item(1, 4).text().startswith("ファイルあり")
    view.filter_edit.setText("DOC")
    assert view.version_table.rowCount() == 1
    view.filter_edit.setText("zzz")
    assert view.version_table.isHidden() and "該当する版がありません" in view.version_empty.text()


def test_history_adopt_conflict_with_confirmation(rig):
    view = _configure_history(rig)
    rig.window.show_page("history")
    view.keep_conflict_button.click()
    assert rig.confirms[-1] == "競合の解決"
    assert (rig.folder / "doc.txt").read_text(encoding="utf-8") == "競合コピー"
    assert view.conflict_table.rowCount() == 0 and not view.conflict_empty.isHidden()
    assert "解決しました" in view.status.text()
    kept = [p.read_text(encoding="utf-8") for p in (rig.folder / ".stversions").rglob("*") if p.is_file()]
    assert kept == ["いまの版"]  # 選ばなかったほうは消えずに過去の版へ


def test_history_cancel_confirmation_changes_nothing(qapp, tmp_path):
    rig = Rig(tmp_path, confirm_answer=False)
    view = _configure_history(rig)
    rig.window.show_page("history")
    view.keep_original_button.click()
    view.tabs.setCurrentIndex(1)
    view.version_table.selectRow(0)
    view.restore_button.click()
    assert rig.confirms == ["競合の解決", "過去の版に戻す"]
    assert (rig.folder / CONFLICT_COPY).exists()
    assert rig.client.called("restore_versions") == []


def test_history_restore_version(rig):
    view = _configure_history(rig)
    rig.window.show_page("history")
    view.version_table.selectRow(0)
    view.restore_button.click()
    assert rig.client.called("restore_versions") == [
        ("folderlink-hist", {"gone.bin": "2026-11-21T13:00:07+09:00"})]
    assert "戻しました" in view.status.text()
    rig.client.restore_errors = {"gone.bin": "denied"}
    view.version_table.selectRow(0)
    view.restore_button.click()
    assert not view.banner.isHidden() and "復元できませんでした" in view.banner.text.text()
    assert view.restore_button.isEnabled()  # 失敗しても操作できる状態に戻る


def _trip(applied=0, paused=True):
    from datetime import datetime, timedelta, timezone

    from folderlink.core.safety_guard import GuardTrip

    # 見本の過去の版（13:00:07+09:00 = 04:00:07 UTC）より前に削除が届き始めたことにする
    first = datetime(2026, 11, 21, 3, 59, tzinfo=timezone.utc)
    return GuardTrip("folderlink-hist", first + timedelta(minutes=2), first, 280, applied, paused)


def test_guard_runs_only_when_configured_and_enabled(rig):
    assert rig.service._guard is None  # 未設定のあいだは動かない
    s = rig.service.settings
    s.setup_completed, s.folder_path, s.folder_id = True, str(rig.folder), "folderlink-hist"
    rig.service._rebuild_collector()
    assert rig.service._guard is not None and rig.service._guard.running
    assert rig.service._guard.counter.threshold == s.mass_delete_threshold
    rig.service.save_settings(mass_delete_guard_enabled=False)
    assert rig.service._guard is None
    rig.service.save_settings(mass_delete_guard_enabled=True, mass_delete_threshold=7)
    assert rig.service._guard.counter.threshold == 7
    rig.service.stop()
    assert rig.service._guard is None


def test_guard_trip_banner_and_accept(rig):
    view = _configure_history(rig)
    rig.client.folders["folderlink-hist"] = {"id": "folderlink-hist", "paused": True}
    rig.service.guard_trip = _trip()
    rig.window.apply_settings(rig.service.settings)
    rig.controller.refresh()
    dash = rig.window.dashboard
    assert not dash.banner.isHidden() and "まだ削除せず" in dash.banner.text.text()
    assert "確実に防ぐ機能ではありません" in dash.guard_label.text() and "保護" not in dash.guard_label.text()
    dash.banner.button.click()
    assert rig.window.current_page() == "history"
    assert not view.guard_box.isHidden() and view.guard_accept_button.isEnabled()
    view.guard_accept_button.click()
    assert rig.confirms[-1] == "削除を受け入れる"
    assert rig.service.guard_trip is None
    assert rig.client.folders["folderlink-hist"]["paused"] is False
    assert view.guard_box.isHidden() and "再開しました" in view.status.text()


def test_guard_restore_brings_back_deleted_files(rig):
    view = _configure_history(rig)
    rig.client.folders["folderlink-hist"] = {"id": "folderlink-hist", "paused": True}
    rig.client.status["folderlink-hist"] = {"state": "idle", "needDeletes": 0}
    trip = _trip(applied=12)
    rig.service.guard_trip = trip
    rig.controller.refresh()
    rig.window.show_page("history")
    assert "止まる前に" in view.guard_banner.text.text()
    view.guard_restore_button.click()
    assert rig.confirms[-1] == "削除されたファイルを戻す"
    # 検知より後に退避され、いま無いファイル（gone.bin）だけを戻す。いまある doc.txt は戻さない
    assert rig.client.called("restore_versions") == [
        ("folderlink-hist", {"gone.bin": "2026-11-21T13:00:07+09:00"})]
    assert rig.client.folders["folderlink-hist"]["paused"] is False
    assert rig.service.guard_trip is None
    assert "件のファイルを戻しました" in view.status.text()


def test_history_not_configured_shows_error(rig):
    view = rig.pages["history"]
    rig.window.show_page("history")
    assert not view.banner.isHidden() and "設定がありません" in view.banner.text.text()
