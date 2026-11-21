"""結合テスト（1台2インスタンス）: 過去の版からの復元と、競合の解決を FolderLink のコードで行う。

これは「1台の中での確認」。実機2台・別ネットワークでの受入テストの代わりではない。
"""

from __future__ import annotations

import os
import time

from it_helpers import wait_until
from folderlink.core import history


def write(path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)


def test_versioning_is_staggered_30_days(new_pair):
    p = new_pair().start()
    v = p.a.client.get_folder(p.folder_id).get("versioning") or {}
    assert v.get("type") == "staggered"
    assert v.get("params", {}).get("maxAge") == str(30 * 86400)  # 単位は秒（実物で確認）


def test_delete_on_a_then_restore_on_b(new_pair):
    """A で消したファイルを、B の過去の版から戻す → 両側に戻る。"""
    p = new_pair().start()
    p.wait_connected()
    data = os.urandom(150_000)
    write(p.a.folder / "sub" / "data.bin", data)
    p.settle()
    os.remove(p.a.folder / "sub" / "data.bin")
    p.settle()
    assert not (p.b.folder / "sub" / "data.bin").exists()

    # A には過去の版ができない（自分で消したので）。B にできる。
    assert history.list_versions(p.a.client, p.folder_id, p.a.folder) == []
    versions = history.list_versions(p.b.client, p.folder_id, p.b.folder)
    entry = next(v for v in versions if v.rel_path == "sub/data.bin")
    assert entry.current_exists is False and entry.size == len(data)
    used, count = history.versions_disk_usage(p.b.folder)
    assert used >= len(data) and count >= 1

    history.restore_version(p.b.client, p.folder_id, entry.rel_path, entry.version_time, p.b.folder)
    wait_until(lambda: (p.a.folder / "sub" / "data.bin").exists(), 60, desc="A にも戻る")
    p.settle()
    assert (p.a.folder / "sub" / "data.bin").read_bytes() == data
    assert (p.b.folder / "sub" / "data.bin").read_bytes() == data


def test_overwrite_then_restore_keeps_current_version(new_pair):
    """上書きされた版に戻すとき、いまの版は消えずに過去の版として残る。"""
    p = new_pair().start()
    p.wait_connected()
    write(p.a.folder / "doc.txt", "古い内容")
    p.settle()
    write(p.a.folder / "doc.txt", "新しい内容")
    p.settle()
    versions = history.list_versions(p.b.client, p.folder_id, p.b.folder)
    entry = next(v for v in versions if v.rel_path == "doc.txt")
    assert entry.current_exists

    # 過去の版ができたのと同じ秒に復元すると、エンジンが選んだ版を失う（実物で確認）。
    # folder_path を渡すと FolderLink が秒の変わり目を待つ。
    history.restore_version(p.b.client, p.folder_id, "doc.txt", entry.version_time, p.b.folder)
    wait_until(lambda: (p.a.folder / "doc.txt").read_text(encoding="utf-8") == "古い内容", 60, desc="A にも戻る")
    p.settle()
    # 戻す前の「新しい内容」は B の過去の版に残っている
    kept = [f.read_text(encoding="utf-8") for f in (p.b.folder / ".stversions").rglob("doc~*.txt")]
    assert "新しい内容" in kept


def _make_conflict(p):
    write(p.a.folder / "doc.txt", "元の内容")
    p.settle()
    p.a.client.pause_device(p.b.device_id)
    p.b.client.pause_device(p.a.device_id)
    wait_until(lambda: not p.connected(), 30, desc="切断")
    write(p.a.folder / "doc.txt", "A の編集（先）")
    time.sleep(2.2)
    write(p.b.folder / "doc.txt", "B の編集（後）")
    p.scan()
    p.a.client.resume_device(p.b.device_id)
    p.b.client.resume_device(p.a.device_id)
    p.wait_connected(90)
    wait_until(lambda: history.list_conflicts(p.a.folder) and history.list_conflicts(p.b.folder), 120,
               desc="競合コピー")
    p.settle(120)
    conflicts = history.list_conflicts(p.a.folder)
    assert len(conflicts) == 1 and conflicts[0].original_rel_path == "doc.txt"
    return conflicts[0]


def _non_version_texts(folder):
    return sorted(f.read_text(encoding="utf-8") for f in folder.iterdir() if f.is_file() and f.suffix == ".txt")


def test_conflict_adopt_copy(new_pair):
    """競合コピーを採用 → 両側の doc.txt が A の編集になり、B の編集は過去の版に残る。"""
    p = new_pair().start()
    p.wait_connected()
    conflict = _make_conflict(p)
    assert (p.a.folder / conflict.rel_path).read_text(encoding="utf-8") == "A の編集（先）"

    result = history.resolve_conflict(p.a.client, p.folder_id, p.a.folder, conflict.rel_path, "conflict")
    assert result["archived"]
    wait_until(lambda: (p.b.folder / "doc.txt").read_text(encoding="utf-8") == "A の編集（先）", 90,
               desc="B にも反映")
    p.settle(120)
    for folder in (p.a.folder, p.b.folder):
        assert _non_version_texts(folder) == ["A の編集（先）"]
        assert history.list_conflicts(folder) == []
    # 選ばなかった B の編集は、A では自分で退避した過去の版として残る
    kept_a = [f.read_text(encoding="utf-8") for f in (p.a.folder / ".stversions").rglob("doc~*.txt")]
    assert "B の編集（後）" in kept_a
    # A の過去の版は API の一覧にも出る（画面から戻せる）
    assert any(v.rel_path == "doc.txt" for v in history.list_versions(p.a.client, p.folder_id, p.a.folder))


def test_conflict_keep_original(new_pair):
    """いまの版を残す → 競合コピーは両側から消えるが、A の過去の版に残る。"""
    p = new_pair().start()
    p.wait_connected()
    conflict = _make_conflict(p)
    history.resolve_conflict(p.a.client, p.folder_id, p.a.folder, conflict.rel_path, "original")
    wait_until(lambda: history.list_conflicts(p.b.folder) == [], 90, desc="B の競合コピーも消える")
    p.settle(120)
    for folder in (p.a.folder, p.b.folder):
        assert _non_version_texts(folder) == ["B の編集（後）"]
    kept_a = [f.read_text(encoding="utf-8") for f in (p.a.folder / ".stversions").rglob("*.txt")]
    assert "A の編集（先）" in kept_a
    # B 側でも、届いた削除の前の版が過去の版に残る
    kept_b = [f.read_text(encoding="utf-8") for f in (p.b.folder / ".stversions").rglob("*.txt")]
    assert "A の編集（先）" in kept_b
