"""履歴画面（競合・過去の版）の見本画像。データは見本用の作りもの（ファイルには触れない）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from folderlink.devtools.ui_shots import _save


def _sample_data():
    from folderlink.core.conflicts import ConflictFile
    from folderlink.core.engine_service import HistoryData
    from folderlink.core.history import VersionEntry

    jst = timezone(timedelta(hours=9))
    base = datetime(2026, 11, 21, 12, 34, 56, tzinfo=jst)
    conflicts = [
        ConflictFile("Assets/Scenes/Main.sync-conflict-20261121-123012-P56IOI7.unity",
                     "Assets/Scenes/Main.unity", datetime(2026, 11, 21, 12, 30, 12), "P56IOI7",
                     1_482_113, (base - timedelta(minutes=5)).timestamp(), True, 1_490_220,
                     (base - timedelta(minutes=2)).timestamp()),
        ConflictFile("企画書.sync-conflict-20261120-181501-P56IOI7.docx", "企画書.docx",
                     datetime(2026, 11, 20, 18, 15, 1), "P56IOI7", 48_211,
                     (base - timedelta(hours=18)).timestamp(), True, 49_870,
                     (base - timedelta(hours=17)).timestamp()),
    ]
    versions = [
        VersionEntry("動画/撮影_0412.mp4", "t1", base - timedelta(minutes=12), base - timedelta(days=2),
                     2_621_440_000, False),
        VersionEntry("Assets/Scripts/Player.cs", "t2", base - timedelta(hours=1), base - timedelta(hours=3),
                     18_204, True),
        VersionEntry("企画書.docx", "t3", base - timedelta(days=1), base - timedelta(days=1, hours=2),
                     47_002, True),
        VersionEntry("メモ/会議メモ.txt", "t4", base - timedelta(days=6), base - timedelta(days=8), 1_204, False),
    ]
    return HistoryData(conflicts, versions, 2_621_440_000 + 18_204 + 47_002 + 1_204 + 1_530_324, 6)


def shoot(app, out: Path, theme_name: str, suffix: str, make_window) -> None:
    from folderlink.core.engine_service import HistoryData

    tag = f"{theme_name}{suffix}"
    window, pages = make_window(app, theme_name)
    view = pages["history"]
    window.show_page("history")
    view.set_data(_sample_data())
    _save(window, out / f"history_conflicts_{tag}.png")
    view.tabs.setCurrentIndex(1)
    _save(window, out / f"history_versions_{tag}.png")
    # 大量削除を検知したとき
    from datetime import datetime, timezone

    from folderlink.core.safety_guard import GuardTrip

    now = datetime(2026, 11, 21, 3, 40, tzinfo=timezone.utc)
    view.tabs.setCurrentIndex(0)
    view.set_guard_trip(GuardTrip("fl-sample", now, now, 300, 0, True))
    _save(window, out / f"history_guard_{tag}.png")
    if theme_name == "light":
        view.set_guard_trip(GuardTrip("fl-sample", now, now, 85, 27, True))
        _save(window, out / f"history_guard_partial_{tag}.png")
    view.set_guard_trip(None)
    if theme_name == "light":
        view.set_data(HistoryData([], [], 0, 0))
        view.tabs.setCurrentIndex(0)
        _save(window, out / f"history_empty_{tag}.png")
    window.close()
