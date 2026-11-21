"""結合テスト: 1台の中で A・B 2つの Syncthing を立て、FolderLink のコードでペアリング・共有する。

これは「1台の中での確認」。実機2台・別ネットワークでの受入テストの代わりではない。
"""

from __future__ import annotations

import os
import time

import pytest

from it_helpers import sha256_file, tree, wait_until
from folderlink.core import pairing as pg
from folderlink.core.api_client import ApiError
from folderlink.core.sync_status import StatusCollector, SyncState


def write(path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)


# ---------------------------------------------------------------- 使い回しの A・B
def test_pairing_and_real_device_ids(shared_pair):
    p = shared_pair
    # 実物が作ったデバイス ID が、こちらの検算（Luhn mod 32）を通る
    for node in p.nodes:
        assert pg.normalize_device_id(node.device_id) == node.device_id
        raw = node.device_id.replace("-", "")
        without_check = "".join(raw[i:i + 13] for i in range(0, 56, 14))
        assert pg.normalize_device_id(without_check) == node.device_id
    # 自分自身の ID は登録できない
    with pytest.raises(pg.DeviceIdError):
        p.pa.add_device(p.a.device_id)
    # 接続中で、中継ではなく直接つながっている
    state = p.pa.peer_state(p.b.device_id)
    assert state.configured and state.connected and state.via_relay is False
    # 同じフォルダ ID、PC ごとに違うパス、過去版の保持つき
    fa, fb = p.a.client.get_folder(p.folder_id), p.b.client.get_folder(p.folder_id)
    assert fa["path"] != fb["path"]
    for cfg in (fa, fb):
        assert cfg["versioning"]["type"] == "staggered"
        assert cfg["versioning"]["params"]["maxAge"] == str(30 * 86400)
        assert cfg["maxConflicts"] != 0 and cfg["ignoreDelete"] is False
        assert {d["deviceID"] for d in cfg["devices"]} == {p.a.device_id, p.b.device_id}
    # 承認待ちは残っていない
    assert p.pa.pending_devices() == [] and p.pb.pending_folders() == []


def test_create_update_rename_delete(shared_pair):
    p = shared_pair
    a, b = p.a.folder, p.b.folder

    # 新規作成（A → B）。日本語の名前とサブフォルダ
    write(a / "メモ.txt", "こんにちは")
    write(a / "sub" / "深い" / "data.bin", os.urandom(300_000))
    p.scan(p.a)
    t = p.wait_trees_equal()
    assert "メモ.txt" in t and "sub/深い/data.bin" in t
    assert (b / "メモ.txt").read_text(encoding="utf-8") == "こんにちは"

    # 新規作成（B → A）: 双方向
    write(b / "from_b.txt", "B で作成")
    p.scan(p.b)
    p.wait_trees_equal()
    assert (a / "from_b.txt").read_text(encoding="utf-8") == "B で作成"

    # 更新
    write(a / "メモ.txt", "こんにちは\n追記しました")
    p.scan(p.a)
    p.wait_trees_equal()
    assert (b / "メモ.txt").read_text(encoding="utf-8") == "こんにちは\n追記しました"

    # 名前変更（ファイルとフォルダ）
    os.rename(a / "メモ.txt", a / "メモ_改名.txt")
    os.rename(a / "sub", a / "sub_renamed")
    p.scan(p.a)
    t = p.wait_trees_equal()
    assert "メモ_改名.txt" in t and "メモ.txt" not in t
    assert "sub_renamed/深い/data.bin" in t and not any(k.startswith("sub/") for k in t)

    # 削除（B で消す → A からも消える）
    os.remove(b / "from_b.txt")
    p.scan(p.b)
    p.wait_trees_equal()
    assert not (a / "from_b.txt").exists()

    p.wait_synced()
    # 状態の判定も「同期完了」になる
    snap = StatusCollector(p.a.client, p.folder_id, p.b.device_id).collect()
    assert snap.state is SyncState.SYNCED, snap
    assert snap.completion == 100.0 and snap.need_items == 0
    assert snap.peer_connected is True and snap.last_sync is not None
    assert snap.global_files is not None and snap.global_files >= 2


def test_ignore_presets_take_effect(shared_pair):
    p = shared_pair
    a, b = p.a.folder, p.b.folder
    # Unity プロジェクトの形を A に作る
    write(a / "Assets" / "Scenes" / "Main.unity", "scene")
    write(a / "Assets" / "Plugins" / "Library" / "keep.dll", "同名フォルダは同期する")
    write(a / "Packages" / "manifest.json", "{}")
    write(a / "ProjectSettings" / "ProjectVersion.txt", "6000.0")
    write(a / "Library" / "ArtifactDB", os.urandom(50_000))
    write(a / "Temp" / "x.tmp", "t")
    write(a / "Logs" / "Editor.log", "log")
    write(a / "Obj" / "a.o", "o")
    write(a / "Builds" / "game.exe", "exe")
    write(a / ".vs" / "cfg.json", "{}")
    write(a / "Assembly-CSharp.csproj", "<Project/>")
    write(a / "Assets" / "Thumbs.db", "junk")
    write(a / "sentinel.txt", "最後に置く目印")
    p.scan(p.a)
    wait_until(lambda: (b / "sentinel.txt").exists() and (b / "Assets" / "Scenes" / "Main.unity").exists(),
               90, desc="除外されないファイルが B に届く")
    p.wait_synced()
    tb = tree(b)
    for synced in ("Assets/Scenes/Main.unity", "Assets/Plugins/Library/keep.dll", "Packages/manifest.json",
                   "ProjectSettings/ProjectVersion.txt", "sentinel.txt"):
        assert synced in tb, synced
    for ignored in ("Library", "Temp", "Logs", "Obj", "Builds", ".vs"):
        assert not (b / ignored).exists(), f"{ignored} が同期されてしまった"
    assert not (b / "Assembly-CSharp.csproj").exists()
    assert not (b / "Assets" / "Thumbs.db").exists()
    # 除外したものは A には残っている（消していない）
    assert (a / "Library" / "ArtifactDB").exists() and (a / "Temp" / "x.tmp").exists()
    # エンジンが読んでいる除外行が、こちらの書いたものと一致する
    assert "/Library" in p.a.client.get_ignores(p.folder_id)
    # 後片づけ（他のテストの比較に影響しないよう、除外されたものを A から消す）
    import shutil

    for name in ("Library", "Temp", "Logs", "Obj", "Builds", ".vs"):
        shutil.rmtree(a / name)
    os.remove(a / "Assembly-CSharp.csproj")
    os.remove(a / "Assets" / "Thumbs.db")
    p.settle()


def test_100mb_file_hash_matches(shared_pair):
    p = shared_pair
    size = 100 * 1024 * 1024
    big = p.a.folder / "big" / "video_100MB.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    with open(big, "wb") as fh:
        for _ in range(100):
            fh.write(os.urandom(1024 * 1024))
    assert big.stat().st_size == size
    src_hash = sha256_file(big)
    t0 = time.monotonic()
    p.scan(p.a)
    dst = p.b.folder / "big" / "video_100MB.bin"
    wait_until(lambda: dst.exists() and dst.stat().st_size == size, 300, desc="100MB のファイルが B に届く")
    p.wait_synced(120)
    elapsed = time.monotonic() - t0
    assert sha256_file(dst) == src_hash
    print(f"100MB の転送と照合: {elapsed:.1f} 秒")
    # 一部だけ書き換えても（差分同期）、最終的にハッシュが一致する
    with open(big, "r+b") as fh:
        fh.seek(50 * 1024 * 1024)
        fh.write(b"PATCHED-BY-TEST" * 100)
    new_hash = sha256_file(big)
    assert new_hash != src_hash
    p.scan(p.a)
    wait_until(lambda: sha256_file(dst) == new_hash, 300, desc="書き換え後のハッシュ一致")
    os.remove(big)
    p.settle()


def test_pause_and_resume_and_paused_api_behaviour(shared_pair):
    """一時停止中の API の実際の応答を記録し、状態が「一時停止」になることを確かめる。"""
    p = shared_pair
    p.wait_synced()
    p.pa.set_folder_paused(p.folder_id, True)
    try:
        observed = {}
        for name, call in (("db_status", lambda: p.a.client.db_status(p.folder_id)),
                           ("db_completion", lambda: p.a.client.db_completion(p.folder_id, p.b.device_id))):
            try:
                observed[name] = ("OK", call())
            except ApiError as exc:
                observed[name] = (type(exc).__name__, exc.status, str(exc))
        print("一時停止中の応答:", observed)
        snap = StatusCollector(p.a.client, p.folder_id, p.b.device_id).collect()
        assert snap.state is SyncState.PAUSED
        assert snap.completion is None and snap.need_items is None
        # 設定を見ずに問い合わせても、落ちずに「一時停止」と判定できる
        raw = StatusCollector(p.a.client, p.folder_id, p.b.device_id)
        orig = p.a.client.get_folder
        try:
            p.a.client.get_folder = lambda fid: {**orig(fid), "paused": False}  # type: ignore[method-assign]
            snap2 = raw.collect()
        finally:
            p.a.client.get_folder = orig  # type: ignore[method-assign]
        print("設定を見ない場合の判定:", snap2.state, snap2.message)
        assert snap2.state is SyncState.PAUSED
        # 一時停止中の「全部 0」の応答を、件数・容量として表示しない
        assert snap2.global_files is None and snap2.global_bytes is None and snap2.completion is None
        # 実物の応答の形（Syncthing を上げたときに変わったら気づけるように固定）
        assert observed["db_completion"][0] == "ApiFolderPausedError"
        assert observed["db_status"][0] == "OK" and observed["db_status"][1]["state"] == ""
        # 一時停止中に B で作ったファイルは A に届かない
        write(p.b.folder / "while_paused.txt", "停止中")
        p.scan(p.b)
        time.sleep(4)
        assert not (p.a.folder / "while_paused.txt").exists()
    finally:
        p.pa.set_folder_paused(p.folder_id, False)
    # 再開すると届く
    wait_until(lambda: (p.a.folder / "while_paused.txt").exists(), 90, desc="再開後に届く")
    p.settle()
    assert StatusCollector(p.a.client, p.folder_id, p.b.device_id).collect().state is SyncState.SYNCED


# ---------------------------------------------------------------- テストごとの A・B
def test_offline_catch_up(new_pair):
    """片方を止めて変更 → 再開後に追いつく。"""
    p = new_pair().start()
    p.wait_connected()
    write(p.a.folder / "keep.txt", "v1")
    write(p.a.folder / "to_delete.txt", "消される予定")
    write(p.a.folder / "to_rename.txt", "名前が変わる予定")
    p.settle()

    p.b.stop()  # B のPCの電源が切れた想定
    wait_until(lambda: not p.pa.peer_state(p.b.device_id).connected, 30, desc="A が切断に気づく")
    snap = StatusCollector(p.a.client, p.folder_id, p.b.device_id).collect()
    assert snap.state is SyncState.OFFLINE and snap.peer_connected is False

    write(p.a.folder / "keep.txt", "v2（B が止まっている間に更新）")
    write(p.a.folder / "new_while_offline.txt", "新規")
    os.remove(p.a.folder / "to_delete.txt")
    os.rename(p.a.folder / "to_rename.txt", p.a.folder / "renamed.txt")
    p.scan(p.a)
    # B のフォルダは止まっている間、変わらない
    assert (p.b.folder / "to_delete.txt").exists()
    assert (p.b.folder / "keep.txt").read_text(encoding="utf-8") == "v1"

    p.b.start()
    p.rebind()
    p.wait_connected(90)
    t = p.wait_trees_equal(120)
    assert set(t) == {"keep.txt", "new_while_offline.txt", "renamed.txt"}
    assert (p.b.folder / "keep.txt").read_text(encoding="utf-8") == "v2（B が止まっている間に更新）"
    p.wait_synced()


def test_simultaneous_edit_keeps_both_versions(new_pair):
    """両側で同じファイルを編集 → 競合コピーが残り、どちらの内容も失われない。"""
    p = new_pair().start()
    p.wait_connected()
    write(p.a.folder / "doc.txt", "元の内容")
    write(p.a.folder / "other.txt", "無関係")
    p.settle()

    # 接続を切る（両側で一時停止）→ 両側で別々に編集
    p.a.client.pause_device(p.b.device_id)
    p.b.client.pause_device(p.a.device_id)
    wait_until(lambda: not p.connected(), 30, desc="切断")
    text_a, text_b = "A で編集した内容（先）", "B で編集した内容（後）"
    write(p.a.folder / "doc.txt", text_a)
    time.sleep(2.2)  # 更新時刻に差をつける（B のほうが新しい）
    write(p.b.folder / "doc.txt", text_b)
    p.scan()

    p.a.client.resume_device(p.b.device_id)
    p.b.client.resume_device(p.a.device_id)
    p.wait_connected(90)

    def conflict_files(folder):
        return sorted(f.name for f in folder.iterdir() if ".sync-conflict-" in f.name)

    wait_until(lambda: conflict_files(p.a.folder) and conflict_files(p.b.folder), 120, desc="競合コピーの出現")
    p.wait_trees_equal(120)
    p.wait_synced()
    for folder in (p.a.folder, p.b.folder):
        names = conflict_files(folder)
        assert len(names) == 1, names
        assert names[0].startswith("doc.sync-conflict-") and names[0].endswith(".txt")
        contents = {(folder / "doc.txt").read_text(encoding="utf-8"),
                    (folder / names[0]).read_text(encoding="utf-8")}
        # どちらの編集も残っている（片方が黙って消えていない）
        assert contents == {text_a, text_b}
        # 新しいほう（B）が元の名前、古いほう（A）が競合コピー
        assert (folder / "doc.txt").read_text(encoding="utf-8") == text_b
        assert (folder / "other.txt").read_text(encoding="utf-8") == "無関係"
    print("競合コピーの名前:", conflict_files(p.a.folder))


def test_initial_sync_of_two_non_empty_folders_merges_without_loss(new_pair):
    """最初から両方にファイルがある状態で繋いでも、どちらのファイルも消えない。"""
    p = new_pair()
    write(p.a.folder / "only_a.txt", "A だけにある")
    write(p.b.folder / "only_b.txt", "B だけにある")
    write(p.a.folder / "same.txt", "同じ内容")
    write(p.b.folder / "same.txt", "同じ内容")
    write(p.a.folder / "differs.txt", "A の版")
    time.sleep(1.5)
    write(p.b.folder / "differs.txt", "B の版")
    p.start()
    p.wait_connected()
    p.scan()
    wait_until(lambda: (p.a.folder / "only_b.txt").exists() and (p.b.folder / "only_a.txt").exists(), 90,
               desc="両側のファイルが行き渡る")
    t = p.wait_trees_equal(120)
    p.wait_synced()
    assert {"only_a.txt", "only_b.txt", "same.txt", "differs.txt"} <= set(t)
    for folder in (p.a.folder, p.b.folder):
        everything = {f.read_text(encoding="utf-8") for f in folder.iterdir() if f.is_file() and f.suffix == ".txt"}
        assert {"A だけにある", "B だけにある", "同じ内容", "A の版", "B の版"} <= everything
    # 内容が同じファイルは競合にならない
    assert not [k for k in t if k.startswith("same.sync-conflict-")]
    print("初回同期後の内容:", sorted(t))
