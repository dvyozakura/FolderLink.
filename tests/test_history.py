"""履歴（過去の版・競合の解決）の単体テスト。エンジンは偽物、ファイルは tmp_path の中だけ。"""

from __future__ import annotations

import os
import time

import pytest

from fake_client import FakeClient
from folderlink.core import history
from folderlink.core.api_client import ApiConnectionError

FID = "folderlink-test"
DEV = "ABCDEFG"


def write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def conflict_name(stem: str, ext: str = ".txt") -> str:
    return f"{stem}.sync-conflict-20261121-123456-{DEV}{ext}"


# ---------------------------------------------------------------- 過去の版の一覧
def test_list_versions_newest_first_and_marks_deleted(tmp_path):
    client = FakeClient()
    client.versions[FID] = {
        "doc.txt": [{"versionTime": "2026-11-21T12:59:01+09:00", "modTime": "2026-11-21T12:58:58+09:00", "size": 2}],
        "sub/data.bin": [{"versionTime": "2026-11-21T13:00:07+09:00", "modTime": "2026-11-21T12:58:58+09:00",
                          "size": 200000},
                         {"versionTime": "2026-11-21T12:00:00+09:00", "modTime": None, "size": True}],
        "broken": "not-a-list",
    }
    write(tmp_path / "doc.txt", "今の内容")
    entries = history.list_versions(client, FID, tmp_path)
    assert [(e.rel_path, e.version_time[11:19]) for e in entries] == [
        ("sub/data.bin", "13:00:07"), ("doc.txt", "12:59:01"), ("sub/data.bin", "12:00:00")]
    assert entries[0].current_exists is False and entries[0].size == 200000
    assert entries[1].current_exists is True
    assert entries[2].size is None and entries[2].modified_at is None  # 取れない値は捏造しない


def test_list_versions_api_failure_is_history_error(tmp_path):
    client = FakeClient()
    client.fail["folder_versions"] = ApiConnectionError("接続できません")
    with pytest.raises(history.HistoryError, match="過去の版を取得できませんでした"):
        history.list_versions(client, FID, tmp_path)


# ---------------------------------------------------------------- 復元
def test_restore_calls_api_and_rescans(tmp_path):
    client = FakeClient()
    history.restore_version(client, FID, "doc.txt", "2026-11-21T12:59:01+09:00", tmp_path)
    assert client.called("restore_versions") == [(FID, {"doc.txt": "2026-11-21T12:59:01+09:00"})]
    assert client.called("scan")


def test_restore_reports_engine_errors(tmp_path):
    client = FakeClient()
    client.restore_errors = {"doc.txt": "permission denied"}
    with pytest.raises(history.HistoryError, match="使用中"):
        history.restore_version(client, FID, "doc.txt", "t", tmp_path)


def test_restore_rejects_paths_outside_folder(tmp_path):
    client = FakeClient()
    for bad in ("../x.txt", "C:/x.txt", "/x.txt", ".stversions/a~1.txt"):
        with pytest.raises(history.HistoryError):
            history.restore_version(client, FID, bad, "t", tmp_path)
    assert client.called("restore_versions") == []


def test_restore_waits_while_archive_name_of_this_second_is_taken(tmp_path):
    """同じ秒の退避名が既にあると、エンジンが選んだ版を失う（実物で確認）。秒が変わるまで待つ。"""
    clock = {"t": time.mktime((2026, 11, 21, 13, 4, 48, 0, 0, -1)) + 0.2}
    taken = tmp_path / ".stversions" / "sub" / "doc~20261121-130448.txt"
    write(taken, "古い内容")
    slept: list[float] = []

    def sleep(sec: float) -> None:
        slept.append(sec)
        clock["t"] += sec

    client = FakeClient()
    history.restore_version(client, FID, "sub/doc.txt", "2026-11-21T13:04:48+09:00", tmp_path,
                            clock=lambda: clock["t"], sleep=sleep)
    assert slept and sum(slept) >= 0.7  # 13:04:49 になるまで待った
    assert client.called("restore_versions")


def test_restore_gives_up_if_name_stays_taken(tmp_path):
    clock = {"t": 1_000_000.0}

    def sleep(sec: float) -> None:
        clock["t"] += sec
        # 毎回その秒の名前を作り続ける（ありえないが、待ちが終わることを確かめる）
        tag = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock["t"]))
        write(tmp_path / ".stversions" / f"doc~{tag}.txt", "x")

    write(tmp_path / ".stversions" / f"doc~{time.strftime('%Y%m%d-%H%M%S', time.localtime(clock['t']))}.txt", "x")
    client = FakeClient()
    with pytest.raises(history.HistoryError, match="数秒待って"):
        history.restore_version(client, FID, "doc.txt", "t", tmp_path, clock=lambda: clock["t"], sleep=sleep)
    assert client.called("restore_versions") == []


# ---------------------------------------------------------------- 退避
def test_archive_moves_into_stversions_with_engine_naming(tmp_path):
    write(tmp_path / "a" / "b.txt", "内容")
    stamp = time.mktime((2026, 11, 21, 1, 2, 3, 0, 0, -1))
    target = history.archive_to_versions(tmp_path, "a/b.txt", now=stamp)
    assert target == tmp_path / ".stversions" / "a" / "b~20261121-010203.txt"
    assert target.read_text(encoding="utf-8") == "内容"
    assert not (tmp_path / "a" / "b.txt").exists()
    # 同じ秒にもう一度 → 名前をずらして、前のものを上書きしない
    write(tmp_path / "a" / "b.txt", "2回目")
    second = history.archive_to_versions(tmp_path, "a/b.txt", now=stamp)
    assert second != target and target.read_text(encoding="utf-8") == "内容"
    used, count = history.versions_disk_usage(tmp_path)
    assert count == 2 and used == len("内容".encode()) + len("2回目".encode())


def test_archive_missing_file(tmp_path):
    with pytest.raises(history.HistoryError, match="見つかりません"):
        history.archive_to_versions(tmp_path, "nothing.txt")


def test_versions_disk_usage_without_folder(tmp_path):
    assert history.versions_disk_usage(tmp_path) == (0, 0)


# ---------------------------------------------------------------- 競合の解決
def test_keep_original_archives_conflict_copy(tmp_path):
    write(tmp_path / "d" / "doc.txt", "いまの版")
    write(tmp_path / "d" / conflict_name("doc"), "競合コピー")
    client = FakeClient()
    [c] = history.list_conflicts(tmp_path)
    result = history.resolve_conflict(client, FID, tmp_path, c.rel_path, "original")
    assert (tmp_path / "d" / "doc.txt").read_text(encoding="utf-8") == "いまの版"
    assert history.list_conflicts(tmp_path) == []
    kept = [p.read_text(encoding="utf-8") for p in (tmp_path / ".stversions").rglob("*") if p.is_file()]
    assert kept == ["競合コピー"]
    assert result["kept"] == "original" and result["original"] == "d/doc.txt"
    assert client.called("scan")


def test_adopt_conflict_archives_current_then_renames(tmp_path):
    write(tmp_path / "doc.txt", "いまの版")
    write(tmp_path / conflict_name("doc"), "競合コピー")
    history.resolve_conflict(None, FID, tmp_path, conflict_name("doc"), "conflict")
    assert (tmp_path / "doc.txt").read_text(encoding="utf-8") == "競合コピー"
    assert not (tmp_path / conflict_name("doc")).exists()
    kept = [p.read_text(encoding="utf-8") for p in (tmp_path / ".stversions").rglob("*") if p.is_file()]
    assert kept == ["いまの版"]


def test_adopt_conflict_when_original_was_deleted(tmp_path):
    write(tmp_path / conflict_name("doc"), "競合コピー")
    result = history.resolve_conflict(None, FID, tmp_path, conflict_name("doc"), "conflict")
    assert (tmp_path / "doc.txt").read_text(encoding="utf-8") == "競合コピー"
    assert result["archived"] is None


def test_resolve_rejects_bad_input(tmp_path):
    write(tmp_path / "doc.txt", "x")
    with pytest.raises(history.HistoryError):
        history.resolve_conflict(None, FID, tmp_path, "doc.txt", "original")  # 競合コピーではない
    with pytest.raises(history.HistoryError):
        history.resolve_conflict(None, FID, tmp_path, conflict_name("doc"), "both")
    with pytest.raises(history.HistoryError, match="見つかりません"):
        history.resolve_conflict(None, FID, tmp_path, conflict_name("doc"), "original")
    with pytest.raises(history.HistoryError):
        history.resolve_conflict(None, FID, tmp_path, "../" + conflict_name("doc"), "original")
    assert (tmp_path / "doc.txt").read_text(encoding="utf-8") == "x"


def test_adopt_conflict_refuses_when_original_is_directory(tmp_path):
    (tmp_path / "doc.txt").mkdir()
    write(tmp_path / conflict_name("doc"), "競合コピー")
    with pytest.raises(history.HistoryError, match="フォルダ"):
        history.resolve_conflict(None, FID, tmp_path, conflict_name("doc"), "conflict")
    assert (tmp_path / conflict_name("doc")).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows のファイルロック")
def test_locked_file_gives_friendly_error(tmp_path):
    import ctypes
    from ctypes import wintypes

    write(tmp_path / "doc.txt", "いまの版")
    write(tmp_path / conflict_name("doc"), "競合コピー")
    # 共有なし（他から開けない・移動できない）で開いて、使用中の状態を作る
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(str(tmp_path / "doc.txt"), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        with pytest.raises(history.HistoryError, match="使用中"):
            history.resolve_conflict(None, FID, tmp_path, conflict_name("doc"), "conflict")
    finally:
        kernel32.CloseHandle(handle)
    # 失敗しても、どちらのファイルも残っている
    assert (tmp_path / "doc.txt").read_text(encoding="utf-8") == "いまの版"
    assert (tmp_path / conflict_name("doc")).read_text(encoding="utf-8") == "競合コピー"
