"""同期履歴・競合一覧の画面。

- 大量削除を検知したとき: 上に確認の欄が出る（ファイルを戻す／削除を受け入れる）。

- 「競合」タブ: 両方の PC で同じファイルを変更したときに残る競合コピーの一覧。
  どちらを残すかを選べる。選ばなかったほうは消さず、過去の版として残す。
- 「過去の版」タブ: 相手の PC の変更で上書き・削除されたファイルの、直前の版の一覧。選んで戻せる。
  戻すとき、いまあるファイルは先に過去の版として退避される。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from folderlink.core import formatting as fm
from folderlink.core.config_store import Settings
from folderlink.ui import theme
from folderlink.ui.device_panel import default_confirm
from folderlink.ui.widgets import Banner, label, make_button

CONFLICT_HELP = (
    "2台のPCで同じファイルを別々に変更すると、新しいほうが元の名前で残り、古いほうは"
    "「.sync-conflict-…」の付いた競合コピーとして残ります（自動で1つにまとめることはしません）。"
    "中身を見比べてから、残すほうを選んでください。選ばなかったほうは削除されず、"
    "「過去の版」に移ります。"
)
VERSIONS_HELP = (
    "相手のPCの変更で、このPCのファイルが上書き・削除されたときの直前の版です。"
    "このPCで自分が上書き・削除したファイルの直前の版は、相手のPCの「過去の版」にあります。"
    "短い間に何度も上書きした場合、途中の版は残りません（最初の1時間は 30 秒ごとに1つ）。"
)


def _fmt_dt(value: datetime | None) -> str:
    return fm.fmt_time(value, with_date=True)


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.verticalHeader().setVisible(False)
    table.setWordWrap(False)
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for col in range(1, len(headers)):
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
    header.setHighlightSections(False)
    return table


def _item(text: str, tooltip: str | None = None, right: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setToolTip(tooltip if tooltip is not None else text)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


class HistoryView(QWidget):
    def __init__(self, palette: theme.Palette, controller=None, settings: Settings | None = None,
                 confirm: Callable[[QWidget, str, str], bool] = default_confirm,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.controller = controller
        self.settings = settings or Settings()
        self._confirm = confirm
        self._conflicts: list = []
        self._versions: list = []
        self._shown_versions: list = []
        self._busy = False
        self._after_load = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        self.banner = Banner("info")
        self.banner.hide()
        outer.addWidget(self.banner)

        # --- 大量削除の確認（検知したときだけ出る） ---
        self._trip = None
        self.guard_box = QWidget()
        glay = QVBoxLayout(self.guard_box)
        glay.setContentsMargins(0, 0, 0, 0)
        glay.setSpacing(8)
        self.guard_banner = Banner("warn")
        glay.addWidget(self.guard_banner)
        row = QHBoxLayout()
        self.guard_restore_button = make_button("削除されたファイルを戻す", accent=True)
        self.guard_restore_button.setToolTip("過去の版からまとめて戻します。戻したファイルは相手のPCにも戻ります。")
        self.guard_restore_button.clicked.connect(self._guard_restore)
        self.guard_accept_button = make_button("削除を受け入れて同期を再開")
        self.guard_accept_button.setToolTip("相手のPCでの削除を、このPCにも反映します（過去の版には残ります）。")
        self.guard_accept_button.clicked.connect(self._guard_accept)
        row.addWidget(self.guard_restore_button)
        row.addWidget(self.guard_accept_button)
        row.addStretch(1)
        glay.addLayout(row)
        self.guard_box.hide()
        outer.addWidget(self.guard_box)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        # --- 競合 ---
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.addWidget(label(CONFLICT_HELP, "Small", wrap=True))
        self.conflict_table = _table(["ファイル", "競合した日時", "いまの版", "競合コピー", "相手"])
        self.conflict_table.itemSelectionChanged.connect(self._update_buttons)
        lay.addWidget(self.conflict_table, 1)
        self.conflict_empty = label("競合しているファイルはありません。", "Sub")
        lay.addWidget(self.conflict_empty)
        row = QHBoxLayout()
        self.keep_original_button = make_button("いまの版を残す")
        self.keep_original_button.setToolTip("競合コピーを「過去の版」に移します（削除はしません）。")
        self.keep_original_button.clicked.connect(lambda: self._resolve("original"))
        self.keep_conflict_button = make_button("競合コピーを採用する")
        self.keep_conflict_button.setToolTip("いまの版を「過去の版」に移してから、競合コピーを元の名前にします。")
        self.keep_conflict_button.clicked.connect(lambda: self._resolve("conflict"))
        row.addWidget(self.keep_original_button)
        row.addWidget(self.keep_conflict_button)
        row.addStretch(1)
        lay.addLayout(row)
        self.tabs.addTab(page, "競合")

        # --- 過去の版 ---
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.addWidget(label(VERSIONS_HELP, "Small", wrap=True))
        row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("ファイル名で絞り込み")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._fill_versions)
        self.usage_label = label("", "Small")
        row.addWidget(self.filter_edit, 1)
        row.addWidget(self.usage_label)
        lay.addLayout(row)
        self.version_table = _table(["ファイル", "退避された日時", "その版の更新日時", "サイズ", "いまの状態"])
        self.version_table.itemSelectionChanged.connect(self._update_buttons)
        lay.addWidget(self.version_table, 1)
        self.version_empty = label("過去の版はまだありません。", "Sub")
        lay.addWidget(self.version_empty)
        row = QHBoxLayout()
        self.restore_button = make_button("この版に戻す", accent=True)
        self.restore_button.setToolTip("いまあるファイルは、先に「過去の版」へ退避されます。")
        self.restore_button.clicked.connect(self._restore)
        row.addWidget(self.restore_button)
        row.addStretch(1)
        lay.addLayout(row)
        self.tabs.addTab(page, "過去の版")

        row = QHBoxLayout()
        self.refresh_button = make_button("更新")
        self.refresh_button.clicked.connect(self.refresh)
        self.status = label("", "Small", wrap=True)
        row.addWidget(self.refresh_button)
        row.addWidget(self.status, 1)
        outer.addLayout(row)

        if controller is not None:
            controller.actionFinished.connect(self._on_action)
        self._update_buttons()

    # ---- 表示 ----
    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings

    def apply_snapshot(self, snap) -> None:
        self.set_guard_trip(getattr(snap, "guard_trip", None))

    def set_guard_trip(self, trip) -> None:
        from folderlink.core.safety_guard import trip_message

        self._trip = trip
        if trip is None:
            self.guard_box.hide()
            return
        self.guard_banner.set_kind("error" if trip.applied_deletes or not trip.paused else "warn")
        self.guard_banner.set_text(trip_message(trip))
        self.guard_box.show()
        self._update_buttons()

    def on_shown(self) -> None:
        self.status.setText("")
        self.refresh()

    def refresh(self) -> None:
        if self.controller is None:
            return
        self.status.setText("読み込んでいます…")
        self.controller.run("history:load", lambda s: s.history_load())

    def set_data(self, data) -> None:
        self._conflicts = list(data.conflicts)
        self._versions = list(data.versions)
        self._fill_conflicts()
        self._fill_versions()
        self.tabs.setTabText(0, f"競合（{len(self._conflicts)}）" if self._conflicts else "競合")
        self.usage_label.setText(
            f"過去の版が使っている容量: {fm.fmt_bytes(data.versions_bytes)}（{fm.fmt_count(data.versions_files)}）")
        if data.versions_error:
            self._show_banner("error", data.versions_error)
        elif self._conflicts:
            self._show_banner("warn", f"競合したファイルが {len(self._conflicts)} 件あります。どちらを残すか選んでください。")
        else:
            self.banner.hide()
        self._update_buttons()

    def _show_banner(self, kind: str, text: str) -> None:
        self.banner.set_kind(kind)
        self.banner.set_text(text)
        self.banner.show()

    def _fill_conflicts(self) -> None:
        table = self.conflict_table
        table.setRowCount(len(self._conflicts))
        for row, c in enumerate(self._conflicts):
            current = (f"{fm.fmt_bytes(c.original_size)} ・ "
                       f"{_fmt_dt(datetime.fromtimestamp(c.original_mtime)) if c.original_mtime else '—'}"
                       if c.original_exists else "（ありません）")
            copy = f"{fm.fmt_bytes(c.size)} ・ {_fmt_dt(datetime.fromtimestamp(c.mtime))}"
            table.setItem(row, 0, _item(c.original_rel_path, f"{c.original_rel_path}\n競合コピー: {c.rel_path}"))
            table.setItem(row, 1, _item(_fmt_dt(c.when)))
            table.setItem(row, 2, _item(current))
            table.setItem(row, 3, _item(copy))
            table.setItem(row, 4, _item(c.device_short, "競合コピー側を変更したPCの ID の先頭 7 文字"))
        self.conflict_empty.setVisible(not self._conflicts)
        table.setVisible(bool(self._conflicts))
        if self._conflicts:
            table.selectRow(0)

    def _fill_versions(self) -> None:
        needle = self.filter_edit.text().strip().lower()
        self._shown_versions = [v for v in self._versions if not needle or needle in v.rel_path.lower()]
        table = self.version_table
        table.setRowCount(len(self._shown_versions))
        for row, v in enumerate(self._shown_versions):
            table.setItem(row, 0, _item(v.rel_path))
            table.setItem(row, 1, _item(_fmt_dt(v.archived_at)))
            table.setItem(row, 2, _item(_fmt_dt(v.modified_at)))
            table.setItem(row, 3, _item(fm.fmt_bytes(v.size), right=True))
            table.setItem(row, 4, _item("ファイルあり（上書きされた版）" if v.current_exists else "削除済み"))
        self.version_empty.setText("過去の版はまだありません。" if not self._versions else "該当する版がありません。")
        self.version_empty.setVisible(not self._shown_versions)
        table.setVisible(bool(self._shown_versions))
        self._update_buttons()

    def _selected_conflict(self):
        row = self.conflict_table.currentRow()
        if 0 <= row < len(self._conflicts) and self.conflict_table.selectedItems():
            return self._conflicts[row]
        return None

    def _selected_version(self):
        row = self.version_table.currentRow()
        if 0 <= row < len(self._shown_versions) and self.version_table.selectedItems():
            return self._shown_versions[row]
        return None

    def _update_buttons(self) -> None:
        conflict = self._selected_conflict()
        ok = conflict is not None and not self._busy
        self.keep_original_button.setEnabled(ok and conflict.original_exists)
        self.keep_conflict_button.setEnabled(ok)
        self.restore_button.setEnabled(self._selected_version() is not None and not self._busy)
        self.guard_restore_button.setEnabled(self._trip is not None and not self._busy)
        self.guard_accept_button.setEnabled(self._trip is not None and not self._busy)

    # ---- 操作 ----
    def _resolve(self, keep: str) -> None:
        conflict = self._selected_conflict()
        if conflict is None or self.controller is None:
            return
        if keep == "original":
            text = (f"「{conflict.original_rel_path}」は、いまの版を残します。\n\n"
                    "競合コピーは削除せず「過去の版」に移します。あとで戻すこともできます。\n続けますか？")
        else:
            text = (f"「{conflict.original_rel_path}」を、競合コピーの内容に置き換えます。\n\n"
                    "いまの版は削除せず「過去の版」に移します。あとで戻すこともできます。\n"
                    "この変更は相手のPCにも同期されます。続けますか？")
        if not self._confirm(self, "競合の解決", text):
            return
        self._busy = True
        self._update_buttons()
        rel = conflict.rel_path
        self.controller.run("history:resolve", lambda s: s.history_resolve_conflict(rel, keep))

    def _restore(self) -> None:
        version = self._selected_version()
        if version is None or self.controller is None:
            return
        state = "いまあるファイルは、先に「過去の版」へ退避されます。" if version.current_exists \
            else "このファイルはいま存在しないので、選んだ版がそのまま戻ります。"
        if not self._confirm(self, "過去の版に戻す",
                             f"「{version.rel_path}」を、{_fmt_dt(version.archived_at)} に退避された版に戻します。\n\n"
                             f"{state}\nこの変更は相手のPCにも同期されます。続けますか？"):
            return
        self._busy = True
        self._update_buttons()
        rel, vt = version.rel_path, version.version_time
        self.controller.run("history:restore", lambda s: s.history_restore(rel, vt))

    def _guard_restore(self) -> None:
        if self._trip is None or self.controller is None:
            return
        if not self._confirm(self, "削除されたファイルを戻す",
                             "同期を再開し、相手のPCから届いた削除の適用が終わるのを待ってから、"
                             "削除されたファイルを過去の版からまとめて戻します。\n\n"
                             "戻したファイルは相手のPCにも戻ります（相手での削除を取り消します）。\n続けますか？"):
            return
        self._busy = True
        self._update_buttons()
        self.status.setText("ファイルを戻しています…（件数が多いと時間がかかります）")
        self.controller.run("history:guard_restore", lambda s: s.guard_restore())

    def _guard_accept(self) -> None:
        if self._trip is None or self.controller is None:
            return
        if not self._confirm(self, "削除を受け入れる",
                             "相手のPCでの削除を、このPCにも反映して同期を再開します。\n\n"
                             "削除されたファイルは、このPCの「過去の版」に残ります（保持日数のあいだ）。\n続けますか？"):
            return
        self._busy = True
        self._update_buttons()
        self.controller.run("history:guard_accept", lambda s: s.guard_accept())

    def _on_action(self, name: str, ok: bool, result) -> None:
        if not name.startswith("history:"):
            return
        if name == "history:load":
            self.status.setText(self._after_load)
            self._after_load = ""
            if ok:
                self.set_data(result)
            else:
                self._show_banner("error", str(result))
            return
        self._busy = False
        if not ok:
            self._show_banner("error", str(result))
            self._update_buttons()
            return
        if name.startswith("history:guard_"):
            self.set_guard_trip(None)
        # 結果の文面は、続けて行う読み込みが終わったあとも残す
        if name == "history:guard_restore":
            failed = (result or {}).get("failed") or {}
            text = f"{(result or {}).get('restored', 0)} 件のファイルを戻しました。"
            if failed:
                text += f"{len(failed)} 件は戻せませんでした（使用中など）。「過去の版」から個別に戻せます。"
        elif name == "history:guard_accept":
            text = "削除を受け入れ、同期を再開しました。"
        elif name == "history:resolve":
            text = "競合を解決しました。"
        else:
            text = "選んだ版に戻しました。"
        self._after_load = text
        self.refresh()
