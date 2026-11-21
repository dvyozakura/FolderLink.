"""結合テスト（1台2インスタンス、_sandbox/ の中だけ）: 扱いの難しいファイル。

シンボリックリンク・長いパス・ロック中のファイル・大文字小文字だけ違う名前。
結果（挙動の記録）は -s で表示し、TEST_REPORT.md 項目12 に書く。どのテストも「内容が失われない」ことを確かめる。
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

import pytest

from it_helpers import wait_until

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows の挙動を確かめるテスト")


def write(path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def long_path(p) -> str:
    return "\\\\?\\" + str(p)


def folder_errors(node, folder_id):
    try:
        return node.client.folder_errors(folder_id)
    except Exception as exc:  # 取れなくてもテストは続ける
        return [f"(取得できない: {exc!r})"]


def test_symlink_is_not_followed_and_target_untouched(new_pair, workdir):
    p = new_pair().start()
    p.wait_connected()
    outside = workdir / "outside_target.txt"  # 同期フォルダの外（_sandbox の中）
    write(outside, "外のファイル")
    link = p.a.folder / "link_to_outside.txt"
    try:
        os.symlink(outside, link)
    except OSError as exc:
        pytest.skip(f"シンボリックリンクを作れない（権限・開発者モードが無い）: {exc}")
    write(p.a.folder / "normal.txt", "普通のファイル")
    p.scan()
    wait_until(lambda: (p.b.folder / "normal.txt").exists(), 60, desc="普通のファイル")
    time.sleep(3)
    b_link = p.b.folder / "link_to_outside.txt"
    print(f"\n[シンボリックリンク] B にできたか={b_link.exists() or b_link.is_symlink()} "
          f"リンクか={b_link.is_symlink()} A のフォルダのエラー={folder_errors(p.a, p.folder_id)}")
    # リンク先（同期フォルダの外）の中身は変わらない・外のファイルが B にコピーされて実体化しない
    assert outside.read_text(encoding="utf-8") == "外のファイル"
    if b_link.exists() and not b_link.is_symlink():
        pytest.fail("リンク先の中身が実体のファイルとして B に作られた")


def test_directory_junction_does_not_pull_in_outside_files(new_pair, workdir):
    """ジャンクション（管理者権限なしで作れるフォルダのリンク）の先にある、同期フォルダの外のファイルを
    相手に送ってしまわないか。"""
    import _winapi

    p = new_pair().start()
    p.wait_connected()
    outside = workdir / "outside_dir"
    write(outside / "secret.txt", "同期フォルダの外")
    try:
        _winapi.CreateJunction(str(outside), str(p.a.folder / "junction"))
    except OSError as exc:
        pytest.skip(f"ジャンクションを作れない: {exc}")
    write(p.a.folder / "normal.txt", "普通のファイル")
    p.scan()
    wait_until(lambda: (p.b.folder / "normal.txt").exists(), 60, desc="普通のファイル")
    time.sleep(5)
    b_secret = p.b.folder / "junction" / "secret.txt"
    print(f"\n[ジャンクション] 外のファイルが B に届いたか={b_secret.exists()} "
          f"B に junction があるか={(p.b.folder / 'junction').exists()} "
          f"A のエラー={folder_errors(p.a, p.folder_id)}")
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "同期フォルダの外"
    assert not b_secret.exists(), "ジャンクションの先（同期フォルダの外）のファイルが相手に送られた"


def test_long_path_over_260_chars_syncs(new_pair):
    p = new_pair().start()
    p.wait_connected()
    parts = [f"長いフォルダ名_{i:02d}_" + "x" * 30 for i in range(6)]
    rel = os.path.join(*parts, "とても長い名前のファイル.txt")
    src = p.a.folder / rel
    assert len(str(src)) > 260
    os.makedirs(long_path(src.parent), exist_ok=True)
    with open(long_path(src), "w", encoding="utf-8") as f:
        f.write("長いパスの中身")
    p.scan()
    dst = p.b.folder / rel
    try:
        wait_until(lambda: os.path.exists(long_path(dst)), 90, desc="長いパスのファイル")
    finally:
        print(f"\n[長いパス] 文字数={len(str(src))} B に届いたか={os.path.exists(long_path(dst))} "
              f"B のエラー={folder_errors(p.b, p.folder_id)}")
    with open(long_path(dst), encoding="utf-8") as f:
        assert f.read() == "長いパスの中身"


def _lock_exclusive(path):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)  # 共有なしで開く
    assert handle != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    return kernel32, handle


def test_locked_file_on_receiver_catches_up_after_unlock(new_pair):
    p = new_pair().start()
    p.wait_connected()
    write(p.a.folder / "locked.txt", "最初の内容")
    p.settle()
    kernel32, handle = _lock_exclusive(p.b.folder / "locked.txt")
    try:
        write(p.a.folder / "locked.txt", "更新後の内容（ロック中に届く）")
        p.scan()
        time.sleep(8)
        errs = folder_errors(p.b, p.folder_id)
        state = p.b.client.db_status(p.folder_id)
        print(f"\n[ロック中] B のエラー={errs} B の未同期={state.get('needFiles')}")
        assert (p.a.folder / "locked.txt").read_text(encoding="utf-8") == "更新後の内容（ロック中に届く）"
    finally:
        kernel32.CloseHandle(handle)
    # ロックが外れれば追いつく（エンジンは失敗したファイルを後でやり直す）
    p.scan()
    wait_until(lambda: (p.b.folder / "locked.txt").read_text(encoding="utf-8") == "更新後の内容（ロック中に届く）",
               180, desc="ロック解除後の追いつき")
    t0 = time.monotonic()
    p.settle(180)
    print(f"[ロック中] 解除後に追いついた（さらに {time.monotonic() - t0:.1f} 秒で同期完了）")


def test_case_only_rename_propagates(new_pair):
    p = new_pair().start()
    p.wait_connected()
    write(p.a.folder / "Report.txt", "中身")
    p.settle()
    os.rename(p.a.folder / "Report.txt", p.a.folder / "report.txt")
    p.scan()
    wait_until(lambda: "report.txt" in os.listdir(p.b.folder), 90, desc="大文字小文字だけの名前変更")
    p.settle()
    names = [n for n in os.listdir(p.b.folder) if n.lower() == "report.txt"]
    print(f"\n[大文字小文字の名前変更] B の名前={names}")
    assert names == ["report.txt"]
    assert (p.b.folder / "report.txt").read_text(encoding="utf-8") == "中身"


def test_case_only_different_names_on_both_sides_lose_nothing(new_pair):
    """切断中に A が「Memo.txt」、B が「memo.txt」を別々に作る → 再接続後、どちらの内容も失われない。"""
    p = new_pair().start()
    p.wait_connected()
    p.a.client.pause_device(p.b.device_id)
    p.b.client.pause_device(p.a.device_id)
    wait_until(lambda: not p.connected(), 30, desc="切断")
    write(p.a.folder / "Memo.txt", "A の内容")
    time.sleep(1.5)
    write(p.b.folder / "memo.txt", "B の内容")
    p.scan()
    p.a.client.resume_device(p.b.device_id)
    p.b.client.resume_device(p.a.device_id)
    p.wait_connected(90)
    time.sleep(15)
    p.scan()
    time.sleep(5)

    def contents(folder):
        return {f.name: f.read_text(encoding="utf-8") for f in folder.iterdir() if f.is_file()}

    ca, cb = contents(p.a.folder), contents(p.b.folder)
    print(f"\n[大文字小文字の衝突] A={ca} B={cb} A のエラー={folder_errors(p.a, p.folder_id)} "
          f"B のエラー={folder_errors(p.b, p.folder_id)}")
    # 少なくとも、それぞれの PC で自分が書いた内容は消えていない
    assert "A の内容" in ca.values()
    assert "B の内容" in cb.values()
    # FolderLink の画面には、何をすればよいかが日本語で出る
    from folderlink.core.sync_status import StatusCollector

    snap = StatusCollector(p.a.client, p.folder_id, p.b.device_id).collect()
    print(f"[大文字小文字の衝突] A の画面の表示: {snap.state.label} / {snap.message}")
    if snap.state.name == "ERROR":
        assert "大文字と小文字" in snap.message
