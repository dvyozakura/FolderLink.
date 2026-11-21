"""engine_service: 画面から見た一連の流れ（起動→相手の登録→同期開始→一時停止→解除）。エンジンは偽物。"""

from __future__ import annotations

import pytest

from fake_client import MY_ID, FakeClient
from folderlink.core import ignore_presets
from folderlink.core import pairing as pg
from folderlink.core.config_store import ConfigStore
from folderlink.core.engine_service import EngineService
from folderlink.core.sync_status import SyncState
from folderlink.core.syncthing_manager import BinaryMissingError, EngineError

PEER = "P56IOI7-MZJNU2Y-IQGDREY-DM2MGTI-MGL3BXN-PQ6W5BM-TBBZ4TJ-XZWICQ2"


class FakeManager:
    instances: list["FakeManager"] = []
    fail: Exception | None = None

    def __init__(self, home, api_key, **kwargs) -> None:
        self.home = home
        self.api_key = api_key
        self.kwargs = kwargs
        self.client_obj = FakeClient()
        self.alive = False
        self.is_verified = False
        self.stopped = 0
        FakeManager.instances.append(self)

    def ensure_home(self) -> str:
        if FakeManager.fail:
            raise FakeManager.fail
        return MY_ID

    def start(self, cancel=None):
        self.alive = True
        self.is_verified = True
        return self.client_obj

    @property
    def client(self):
        if not self.is_verified:
            raise EngineError("同期エンジンの確認が済んでいません。")
        return self.client_obj

    def is_process_alive(self) -> bool:
        return self.alive

    def exit_code(self):
        return None if self.alive else 2

    def stop(self) -> None:
        self.stopped += 1
        self.alive = False
        self.is_verified = False

    def log_tail(self, lines: int = 50) -> str:
        return "engine log"


@pytest.fixture
def service(tmp_path):
    FakeManager.instances.clear()
    FakeManager.fail = None
    return EngineService(ConfigStore(tmp_path / "data"), manager_factory=FakeManager)


def test_derive_folder_id_is_symmetric_and_valid():
    a = pg.derive_folder_id(MY_ID, PEER)
    assert a == pg.derive_folder_id(PEER, MY_ID) == pg.derive_folder_id(PEER.lower(), MY_ID.replace("-", ""))
    assert a.startswith("fl-") and len(a) == 14 and pg.is_valid_folder_id(a)
    other = pg.derive_folder_id(MY_ID, pg.normalize_device_id("A" * 52))
    assert other != a
    with pytest.raises(pg.DeviceIdError):
        pg.derive_folder_id(MY_ID, MY_ID)


def test_snapshot_before_start_is_starting_not_error(service):
    assert service.snapshot().state is SyncState.STARTING
    assert service.is_running is False


def test_start_failure_is_remembered_for_the_snapshot(service):
    FakeManager.fail = BinaryMissingError("同期エンジン（syncthing.exe）が見つかりません。")
    with pytest.raises(EngineError):
        service.start()
    snap = service.snapshot()
    assert snap.state is SyncState.ERROR and "見つかりません" in snap.message


def test_full_flow(service, tmp_path):
    assert service.start() == MY_ID
    assert service.start() == MY_ID  # 2回呼んでも起動は1回
    assert len(FakeManager.instances) == 1
    manager = FakeManager.instances[0]
    assert manager.kwargs["store"] is service.store
    assert service.snapshot().state is SyncState.NOT_CONFIGURED

    # 相手の ID の検証と登録
    with pytest.raises(pg.DeviceIdError):
        service.check_peer_id(MY_ID)
    assert service.register_peer(PEER.lower(), "PC-B") == PEER
    assert service.settings.peer_device_id == PEER and service.settings.peer_name == "PC-B"
    assert ConfigStore(service.store.base_dir).load_settings().peer_device_id == PEER

    # プレビューと同期開始
    folder = tmp_path / "共有"
    folder.mkdir()
    (folder / "a.txt").write_text("x", encoding="utf-8")
    assert service.preview_folder(str(folder)).files == 1
    fid = service.start_sync(str(folder), presets=["os_junk"], custom_ignores=["*.bak"])
    assert fid == pg.derive_folder_id(MY_ID, PEER)
    cfg = manager.client_obj.folders[fid]
    assert cfg["path"] == str(folder) and cfg["versioning"]["type"] == "staggered"
    # 除外は、フォルダを登録する前にファイルへ書かれている
    lines = ignore_presets.read_stignore(folder)
    assert "*.bak" in lines and "(?d)Thumbs.db" in lines
    s = ConfigStore(service.store.base_dir).load_settings()
    assert s.setup_completed and s.folder_id == fid and s.folder_path == str(folder)
    assert s.ignore_presets == ["os_junk"] and s.custom_ignores == ["*.bak"]

    # 一時停止と再開
    service.set_paused(True)
    assert service.is_paused() is True and service.snapshot().state is SyncState.PAUSED
    service.set_paused(False)
    assert service.is_paused() is False

    # 除外の変更（エンジン経由）
    service.update_ignores(["unity"], ["*.tmp"])
    assert "/Library" in manager.client_obj.ignores[fid] and "*.tmp" in manager.client_obj.ignores[fid]
    assert service.settings.ignore_presets == ["unity"]

    # 解除: 設定だけが消え、ファイルは残る
    service.reset_sync()
    assert fid not in manager.client_obj.folders
    assert (folder / "a.txt").read_text(encoding="utf-8") == "x"
    assert service.settings.setup_completed is False and service.settings.folder_path is None
    assert service.settings.peer_device_id == PEER  # 相手の登録は残す
    service.reset_sync(forget_peer=True)
    assert PEER not in manager.client_obj.devices and service.settings.peer_device_id is None

    service.stop()
    assert manager.stopped == 1 and service.is_running is False


def test_sync_folder_cannot_be_the_apps_own_data_dir(service, tmp_path):
    service.start()
    service.register_peer(PEER)
    for bad in (service.store.base_dir, service.store.syncthing_home, tmp_path):
        with pytest.raises(pg.FolderPathError):
            service.start_sync(str(bad))
    assert FakeManager.instances[0].client_obj.folders == {}


def test_start_sync_requires_peer(service, tmp_path):
    service.start()
    with pytest.raises(pg.PairingError):
        service.start_sync(str(tmp_path))


def test_crash_is_reported(service):
    service.start()
    FakeManager.instances[0].alive = False
    assert service.has_crashed() is True
    snap = service.snapshot()
    assert snap.state is SyncState.ERROR and "停止" in snap.message
    service.stop()
    assert service.has_crashed() is False


def test_operations_before_engine_is_ready_give_friendly_errors(service):
    with pytest.raises(pg.PairingError):
        service.check_peer_id(PEER)
    with pytest.raises(pg.PairingError):
        service.register_peer(PEER)
    with pytest.raises(pg.PairingError):
        service.set_paused(True)
    assert service.pending_devices() == [] and service.peer_state() is None
    assert service.is_paused() is False
