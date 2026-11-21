"""結合テスト: 初期設定の流れ（相手の登録 → 初回同期前プレビュー → 同期開始）を、
画面が使うのと同じ EngineService で、本物の Syncthing 2つに対して行う。"""

from __future__ import annotations

import time

from it_helpers import BINARY, tree, wait_until
from folderlink.core.config_store import ConfigStore
from folderlink.core.engine_service import EngineService
from folderlink.core.pairing import derive_folder_id
from folderlink.core.sync_status import SyncState
from folderlink.core.syncthing_manager import find_free_port


class Side:
    def __init__(self, base, name: str) -> None:
        self.name = name
        self.folder = base / name / "sync"
        self.folder.mkdir(parents=True)
        self.port = find_free_port()
        self.address = f"tcp://127.0.0.1:{self.port}"
        self.store = ConfigStore(base / name / "data")
        self.service = EngineService(self.store, binary=BINARY, isolated=True, listen_address=self.address)
        self.device_id = ""

    def start(self) -> None:
        self.device_id = self.service.start()
        self.service._client().patch_options({"reconnectionIntervalS": 3})


def write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_preview_changes_nothing_then_start_merges_without_loss(workdir, binary, foreign_pids_at_start):
    a, b = Side(workdir, "A"), Side(workdir, "B")
    write(a.folder / "only_a1.txt", "A1")
    write(a.folder / "sub" / "only_a2.txt", "A2")
    write(a.folder / "same.txt", "同じ")
    write(a.folder / "differs.txt", "A の版")
    time.sleep(1.5)
    write(b.folder / "only_b.txt", "B だけ")
    write(b.folder / "same.txt", "同じ")
    write(b.folder / "differs.txt", "B の版（新しい）")
    before_a, before_b = tree(a.folder), tree(b.folder)
    try:
        a.start()
        b.start()
        # ③④ 相手の ID を登録（双方）。外部の探索は切ってあるのでアドレスを直接指定
        a.service.register_peer(b.device_id, "PC-B", addresses=[b.address])
        b.service.register_peer(a.device_id, "PC-A", addresses=[a.address])
        wait_until(lambda: a.service.peer_state().connected and b.service.peer_state().connected, 60,
                   desc="A と B の接続")
        # フォルダ ID は、どちらで計算しても同じ
        fid = derive_folder_id(a.device_id, b.device_id)
        assert a.service.shared_folder_id() == b.service.shared_folder_id() == fid

        # ⑤ プレビュー: A だけが先に進んだ時点では、相手の情報は「分からない」
        assert a.service.prepare_preview(str(a.folder), presets=["os_junk"]) == fid
        info = wait_until(lambda: (lambda i: i if i.local_files == 4 and not i.scanning else None)(
            a.service.preview_info()), 60, desc="A の走査")
        print("A だけ進んだとき:", info)
        assert info.remote_ready is False and info.receive_items is None and info.send_items is None
        assert a.service.snapshot().state is SyncState.NOT_CONFIGURED  # まだ設定完了ではない

        # B もプレビューまで進む → 両方で数字が出る
        assert b.service.prepare_preview(str(b.folder), presets=["os_junk"]) == fid

        def ready(side):
            i = side.service.preview_info()
            return i if (i.remote_ready and not i.scanning and i.receive_items is not None
                         and i.send_items is not None and i.differing is not None
                         and (i.receive_items or 0) + (i.send_items or 0) > 0) else None

        info_a = wait_until(lambda: ready(a), 90, desc="A のプレビュー")
        info_b = wait_until(lambda: ready(b), 90, desc="B のプレビュー")
        time.sleep(3)  # 一覧の交換が落ち着くのを待って、もう一度取る
        info_a, info_b = a.service.preview_info(), b.service.preview_info()
        print("A のプレビュー:", info_a)
        print("B のプレビュー:", info_b)
        assert info_a.local_files == 4 and info_b.local_files == 3
        assert info_a.both_have_files is True and info_b.both_have_files is True

        # **プレビューの間、どちらのフォルダも1バイトも変わっていない**
        assert tree(a.folder) == before_a
        assert tree(b.folder) == before_b
        cfg = a.service._client().get_folder(fid)
        assert cfg["type"] == "sendonly"

        # ⑥ 同期開始（双方）
        a.service.start_sync(str(a.folder))
        b.service.start_sync(str(b.folder))
        assert a.service._client().get_folder(fid)["type"] == "sendreceive"
        assert a.service.settings.setup_completed and b.service.settings.setup_completed
        for side in (a, b):
            side.service._client().scan(fid)

        def merged():
            ta, tb = tree(a.folder), tree(b.folder)
            return ta if ta == tb and "only_b.txt" in ta and "only_a1.txt" in tb else None

        t = wait_until(merged, 120, desc="両方の中身が合わさる")
        wait_until(lambda: a.service.snapshot().state in (SyncState.SYNCED, SyncState.CONFLICT)
                   and b.service.snapshot().state in (SyncState.SYNCED, SyncState.CONFLICT), 90,
                   desc="同期完了")
        print("同期後:", sorted(t))
        # どちらのファイルも消えていない
        assert {"only_a1.txt", "sub/only_a2.txt", "only_b.txt", "same.txt", "differs.txt"} <= set(t)
        for side in (a, b):
            texts = {p.read_text(encoding="utf-8") for p in side.folder.rglob("*.txt")}
            assert {"A1", "A2", "B だけ", "同じ", "A の版", "B の版（新しい）"} <= texts
            assert (side.folder / "differs.txt").read_text(encoding="utf-8") == "B の版（新しい）"
        conflicts = [k for k in t if ".sync-conflict-" in k]
        assert len(conflicts) == 1 and conflicts[0].startswith("differs.sync-conflict-")
        # 内容が違うファイルがあったので、状態は「競合あり」になる（30 秒以内の再計数を待たずに確認）
        a.service.invalidate_conflicts()
        snap = a.service.snapshot()
        assert snap.state is SyncState.CONFLICT and snap.conflicts == 1
    finally:
        a.service.stop()
        b.service.stop()
