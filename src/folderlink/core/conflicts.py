"""競合コピー（`<名前>.sync-conflict-<日付>-<時刻>-<デバイス>.<拡張子>`）を探す。

Syncthing は、両側で同じファイルが変更されたとき、古いほうをこの名前で残す。
競合コピーは普通のファイルとして両方の PC に同期される。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CONFLICT_MARK = ".sync-conflict-"
_SKIP_DIRS = frozenset({".stversions", ".stfolder"})
_PATTERN = re.compile(r"^(?P<stem>.*)\.sync-conflict-(?P<date>\d{8})-(?P<time>\d{6})-(?P<device>[A-Z0-9]{7})(?P<ext>\.[^.]*)?$")


@dataclass(frozen=True)
class ConflictFile:
    rel_path: str            # 競合コピーの場所（同期フォルダからの相対、区切りは /）
    original_rel_path: str   # 元のファイルの場所
    when: datetime | None    # 競合が起きた時刻（ファイル名から）
    device_short: str        # 競合コピー側を変更したデバイスの ID の先頭 7 文字
    size: int
    mtime: float
    original_exists: bool
    original_size: int | None
    original_mtime: float | None


def parse_conflict_name(name: str) -> tuple[str, datetime | None, str] | None:
    """競合コピーのファイル名から（元の名前, 時刻, デバイス）を取り出す。競合コピーでなければ None。"""
    m = _PATTERN.match(name)
    if not m:
        return None
    try:
        when = datetime.strptime(m.group("date") + m.group("time"), "%Y%m%d%H%M%S")
    except ValueError:
        when = None
    return m.group("stem") + (m.group("ext") or ""), when, m.group("device")


def find_conflicts(folder: str | Path, limit: int = 5000) -> list[ConflictFile]:
    """同期フォルダの中の競合コピーを探す（過去版の保管場所 .stversions は見ない）。"""
    root = Path(folder)
    found: list[ConflictFile] = []
    stack = [str(root)]
    while stack and len(found) < limit:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in _SKIP_DIRS:
                        stack.append(entry.path)
                    continue
                if CONFLICT_MARK not in entry.name:
                    continue
                parsed = parse_conflict_name(entry.name)
                if parsed is None:
                    continue
                original_name, when, device = parsed
                st = entry.stat(follow_symlinks=False)
                original = Path(current) / original_name
                try:
                    ost = original.stat()
                    o_exists, o_size, o_mtime = True, ost.st_size, ost.st_mtime
                except OSError:
                    o_exists, o_size, o_mtime = False, None, None
                found.append(ConflictFile(
                    rel_path=Path(entry.path).relative_to(root).as_posix(),
                    original_rel_path=original.relative_to(root).as_posix(),
                    when=when,
                    device_short=device,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    original_exists=o_exists,
                    original_size=o_size,
                    original_mtime=o_mtime,
                ))
            except OSError:
                continue
    found.sort(key=lambda c: (c.when or datetime.min, c.rel_path), reverse=True)
    return found


class ConflictCounter:
    """競合コピーの件数。フォルダ全体を毎回なめると重いので、一定時間は前回の値を使う。"""

    def __init__(self, folder: str | Path, min_interval: float = 30.0, clock=time.monotonic) -> None:
        self.folder = Path(folder)
        self.min_interval = min_interval
        self._clock = clock
        self._last_at: float | None = None
        self._last: int | None = None

    def invalidate(self) -> None:
        self._last_at = None

    def __call__(self) -> int | None:
        now = self._clock()
        if self._last_at is not None and now - self._last_at < self.min_interval:
            return self._last
        if not self.folder.is_dir():
            self._last, self._last_at = None, now
            return None
        self._last = len(find_conflicts(self.folder))
        self._last_at = now
        return self._last
