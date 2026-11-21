"""履歴: 過去の版の一覧と復元、競合コピーの解決。

実物（Syncthing v2.1.7）で確かめた挙動（tests/integration/test_history_restore.py・test_versions_probe.py）:
- 過去の版は、**相手から届いた変更・削除を自分側に適用するとき**に、同期フォルダの中の
  `.stversions/` に `<名前>~<YYYYMMDD-HHMMSS>.<拡張子>` という名前で残る。
- API で復元すると、**いまあるファイルは先に `.stversions/` へ退避されてから**、選んだ版が戻る。
- `.stversions/` に同じ名前の付け方で置いたファイルは、API の一覧に出て、同じように復元できる。

FolderLink の方針: **利用者のファイルを消さない。** 「いらないほう」は削除せず `.stversions/` に
移す（=過去の版として、保持日数のあいだ残る。同じドライブ内の移動なので一瞬で済む）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from folderlink.core.api_client import ApiClient, ApiError
from folderlink.core.conflicts import ConflictFile, find_conflicts, parse_conflict_name
from folderlink.core.sync_status import parse_time

VERSIONS_DIR = ".stversions"
_TAG_FORMAT = "%Y%m%d-%H%M%S"


class HistoryError(Exception):
    """履歴の操作の失敗（文面は利用者に見せてよい日本語）。"""


@dataclass(frozen=True)
class VersionEntry:
    rel_path: str          # 元のファイルの場所（同期フォルダからの相対、区切りは /）
    version_time: str      # API に渡す版の識別子（そのまま返す）
    archived_at: datetime | None   # 版が退避された時刻
    modified_at: datetime | None   # その版の最終更新時刻
    size: int | None
    current_exists: bool   # いま同じ場所にファイルがあるか（無ければ「削除されたファイル」）


def _inside(root: Path, rel_path: str) -> Path:
    """rel_path が同期フォルダの中を指していることを確かめて、絶対パスにする。"""
    if not rel_path or rel_path.startswith(("/", "\\")) or ":" in rel_path:
        raise HistoryError("ファイルの場所が正しくありません。")
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
    if any(p in ("..", ".") for p in parts) or not parts:
        raise HistoryError("ファイルの場所が正しくありません。")
    if parts[0] in (VERSIONS_DIR, ".stfolder"):
        raise HistoryError("この場所のファイルは操作できません。")
    return root.joinpath(*parts)


def list_versions(client: ApiClient, folder_id: str, folder_path: str | Path, limit: int = 5000
                  ) -> list[VersionEntry]:
    """過去の版の一覧（新しい順）。"""
    try:
        raw = client.folder_versions(folder_id)
    except ApiError as exc:
        raise HistoryError(f"過去の版を取得できませんでした（{exc}）。") from None
    root = Path(folder_path)
    entries: list[VersionEntry] = []
    for rel_path, versions in raw.items():
        if not isinstance(versions, list):
            continue
        rel = str(rel_path).replace("\\", "/")
        try:
            exists = (root / rel).exists()
        except OSError:
            exists = False
        for v in versions:
            if not isinstance(v, dict) or not v.get("versionTime"):
                continue
            size = v.get("size")
            entries.append(VersionEntry(
                rel_path=rel,
                version_time=str(v["versionTime"]),
                archived_at=parse_time(v.get("versionTime")),
                modified_at=parse_time(v.get("modTime")),
                size=int(size) if isinstance(size, (int, float)) and not isinstance(size, bool) else None,
                current_exists=exists,
            ))
    entries.sort(key=lambda e: (e.version_time, e.rel_path), reverse=True)
    return entries[:limit]


def _archive_name_for(folder_path: Path, rel_path: str, stamp: float) -> Path:
    rel = Path(*[p for p in rel_path.replace("\\", "/").split("/") if p])
    tag = time.strftime(_TAG_FORMAT, time.localtime(stamp))
    return folder_path / VERSIONS_DIR / rel.parent / f"{rel.stem}~{tag}{rel.suffix}"


def wait_archive_name_free(folder_path: str | Path, rel_path: str, timeout: float = 3.0,
                           clock=time.time, sleep=time.sleep) -> bool:
    """いまの秒の退避名（`<名前>~<いまの時刻>.<拡張子>`）が空くまで待つ。空いたら True。

    実物（v2.1.7）で確認した危険: 過去の版が作られたのと**同じ秒**に復元すると、エンジンは
    いまのファイルを同じ名前で退避して選んだ版を上書きし、その上書きしたものを「復元」する。
    API はエラーを返さないまま、選んだ版の中身が失われる。秒が変わるのを待てば起きない。
    """
    root = Path(folder_path)
    deadline = clock() + timeout
    while True:
        try:
            if not _archive_name_for(root, rel_path, clock()).exists():
                return True
        except OSError:
            return True
        if clock() >= deadline:
            return False
        sleep(0.1)


def restore_version(client: ApiClient, folder_id: str, rel_path: str, version_time: str,
                    folder_path: str | Path | None = None, *, clock=time.time, sleep=time.sleep) -> None:
    """選んだ版に戻す。いまあるファイルは、エンジンが先に `.stversions/` へ退避する（実物で確認済み）。

    folder_path を渡すと、退避名がぶつかって選んだ版が失われる件（wait_archive_name_free）を避ける。
    """
    if not rel_path or not version_time:
        raise HistoryError("復元する版が選ばれていません。")
    if folder_path is not None:
        _inside(Path(folder_path), rel_path)
        if not wait_archive_name_free(folder_path, rel_path, clock=clock, sleep=sleep):
            raise HistoryError("いまは復元できません。数秒待ってからやり直してください。")
    try:
        errors = client.restore_versions(folder_id, {rel_path: version_time})
    except ApiError as exc:
        raise HistoryError(f"復元できませんでした（{exc}）。") from None
    if errors:
        reason = "; ".join(f"{k}: {v}" for k, v in list(errors.items())[:3])
        raise HistoryError(f"復元できませんでした（{reason}）。ファイルが使用中の場合は、閉じてからやり直してください。")
    _rescan(client, folder_id)


def _rescan(client: ApiClient, folder_id: str) -> None:
    try:
        client.scan(folder_id)
    except ApiError:
        pass  # 再スキャンは急がなくてよい（監視と定期スキャンでも拾われる）


def archive_to_versions(folder_path: str | Path, rel_path: str, now: float | None = None) -> Path:
    """ファイルを消す代わりに `.stversions/` へ移す（過去の版として残る）。移した先を返す。"""
    root = Path(folder_path)
    source = _inside(root, rel_path)
    if not source.is_file():
        raise HistoryError("ファイルが見つかりません（既に移動・削除されています）。")
    rel = Path(*[p for p in rel_path.replace("\\", "/").split("/") if p])
    target_dir = root / VERSIONS_DIR / rel.parent
    stamp = now if now is not None else time.time()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        for offset in range(0, 120):
            tag = time.strftime(_TAG_FORMAT, time.localtime(stamp + offset))
            target = target_dir / f"{rel.stem}~{tag}{rel.suffix}"
            if not target.exists():
                break
        else:
            raise HistoryError("退避先の名前を決められませんでした。")
        os.replace(source, target)
    except PermissionError:
        raise HistoryError("ファイルが使用中のため、操作できませんでした。ファイルを閉じてからやり直してください。") from None
    except OSError as exc:
        raise HistoryError(f"ファイルを退避できませんでした（{type(exc).__name__}）。") from None
    return target


def list_conflicts(folder_path: str | Path) -> list[ConflictFile]:
    return find_conflicts(folder_path)


def resolve_conflict(client: ApiClient | None, folder_id: str, folder_path: str | Path, conflict_rel: str,
                     keep: str) -> dict[str, Any]:
    """競合を解決する。

    keep="original": いまの版（元の名前のファイル）を残す。競合コピーは `.stversions/` へ移す。
    keep="conflict": 競合コピーを採用する。いまの版を `.stversions/` へ移してから、競合コピーを元の名前にする。
    どちらの場合も、選ばなかったほうは消えず、過去の版として残る（保持日数のあいだ）。
    """
    if keep not in ("original", "conflict"):
        raise HistoryError("どちらを残すかが正しく指定されていません。")
    root = Path(folder_path)
    conflict_path = _inside(root, conflict_rel)
    parsed = parse_conflict_name(conflict_path.name)
    if parsed is None:
        raise HistoryError("競合コピーではないファイルが指定されました。")
    if not conflict_path.is_file():
        raise HistoryError("競合コピーが見つかりません（既に解決されています）。")
    original_path = conflict_path.with_name(parsed[0])
    original_rel = original_path.relative_to(root).as_posix()
    result: dict[str, Any] = {"kept": keep, "original": original_rel, "archived": None}
    if keep == "original":
        result["archived"] = str(archive_to_versions(root, conflict_rel))
    else:
        if original_path.is_dir():
            raise HistoryError("元の名前の場所がフォルダになっているため、採用できません。")
        if original_path.exists():
            result["archived"] = str(archive_to_versions(root, original_rel))
        try:
            os.replace(conflict_path, original_path)
        except PermissionError:
            raise HistoryError(
                "ファイルが使用中のため、操作できませんでした。ファイルを閉じてからやり直してください。") from None
        except OSError as exc:
            raise HistoryError(f"競合コピーを元の名前に戻せませんでした（{type(exc).__name__}）。") from None
    if client is not None:
        _rescan(client, folder_id)
    return result


def versions_disk_usage(folder_path: str | Path) -> tuple[int, int]:
    """`.stversions/` が使っている容量（バイト）とファイル数。"""
    root = Path(folder_path) / VERSIONS_DIR
    total = count = 0
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                else:
                    total += entry.stat(follow_symlinks=False).st_size
                    count += 1
            except OSError:
                continue
    return total, count
