"""設定の画面: 見た目、自動起動、除外、過去版の保持、大量削除の検知、同期設定の解除、このアプリについて。"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from folderlink import APP_NAME, __version__
from folderlink.core import ignore_presets
from folderlink.core.autostart import AutoStart, AutoStartError
from folderlink.core.config_store import Settings
from folderlink.core.syncthing_manager import SUPPORTED_SYNCTHING
from folderlink.ui import theme
from folderlink.ui.device_panel import default_confirm
from folderlink.ui.widgets import Card, label, make_button

THEME_CHOICES = [("system", "Windows の設定に合わせる"), ("light", "ライト"), ("dark", "ダーク")]

#: 大量削除の検知についての説明。**実際に確かめた範囲だけを書く**（仕様 4章: 検証できるまで「保護あり」と
#: 表示しない）。文面は core/safety_guard.py の検証結果（tests/integration/test_mass_delete_guard.py）に合わせてある。
from folderlink.core.safety_guard import HONEST_DESCRIPTION as GUARD_HONEST_TEXT  # noqa: E402

ABOUT_TEXT = (
    "同期エンジンには Syncthing {st}（Mozilla Public License 2.0）を、改変せずに使用しています。\n"
    "Syncthing のソースコード: https://github.com/syncthing/syncthing/tree/{st}\n"
    "ライセンス文は、インストール先の syncthing フォルダの LICENSE.txt にあります。\n"
    "FolderLink は Syncthing プロジェクトの公式ソフトウェアではありません。"
)

UNITY_NOTE = (
    "Unity を使う場合: 2台で同じプロジェクトを同時に Unity Editor で開く使い方はおすすめしません"
    "（シーンやプレハブが競合し、自動では統合されません）。複数人・複数台での共同作業には、"
    "Git や Unity Version Control の併用をおすすめします。"
)


class SettingsView(QWidget):
    themeChanged = Signal(str)
    resetRequested = Signal()        # 同期設定の解除が済んだ（初期設定へ）
    settingsChanged = Signal(object)  # 保存後の Settings

    def __init__(self, palette: theme.Palette, controller=None, settings: Settings | None = None,
                 autostart: AutoStart | None = None,
                 confirm: Callable[[QWidget, str, str], bool] = default_confirm,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.controller = controller
        self.settings = settings or Settings()
        self.autostart = autostart if autostart is not None else AutoStart()
        self._confirm = confirm
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        # --- 見た目・起動 ---
        card = Card("見た目と起動")
        row = QHBoxLayout()
        row.addWidget(label("テーマ"))
        self.theme_combo = QComboBox()
        for key, text in THEME_CHOICES:
            self.theme_combo.addItem(text, key)
        self.theme_combo.currentIndexChanged.connect(self._on_theme)
        row.addWidget(self.theme_combo)
        row.addStretch(1)
        card.add(row)
        self.autostart_check = QCheckBox("Windows にログインしたとき、自動で起動する（通知領域で起動）")
        self.autostart_check.toggled.connect(self._on_autostart)
        card.add(self.autostart_check)
        self.autostart_note = label("", "Small", wrap=True)
        card.add(self.autostart_note)
        self.minimized_check = QCheckBox("手動で起動したときも、ウィンドウを出さずに通知領域で起動する")
        self.minimized_check.toggled.connect(self._on_minimized)
        card.add(self.minimized_check)
        outer.addWidget(card)

        # --- 除外 ---
        card = Card("同期しないもの（除外）")
        card.add(label("除外の設定は PC ごとに持ちます。相手のPCでも同じ設定にしてください。", "Small", wrap=True))
        self.preset_checks: dict[str, QCheckBox] = {}
        for key, preset in ignore_presets.PRESETS.items():
            check = QCheckBox(preset.name)
            check.setToolTip(preset.description)
            self.preset_checks[key] = check
            card.add(check)
        card.add(label("自分で追加する除外（1行に1つ。例: *.blend1  /作業中  !/Library/keep.txt）", "Small", wrap=True))
        self.custom_edit = QPlainTextEdit()
        self.custom_edit.setFixedHeight(84)
        card.add(self.custom_edit)
        row = QHBoxLayout()
        self.apply_ignores_button = make_button("除外の設定を適用")
        self.apply_ignores_button.clicked.connect(self._apply_ignores)
        self.ignores_status = label("", "Small", wrap=True)
        row.addWidget(self.apply_ignores_button)
        row.addWidget(self.ignores_status, 1)
        card.add(row)
        outer.addWidget(card)

        # --- 過去版 ---
        card = Card("過去の版（履歴）")
        row = QHBoxLayout()
        row.addWidget(label("相手のPCの変更で上書き・削除されたファイルを残す日数"))
        self.days_spin = QSpinBox()
        self.days_spin.setRange(1, 3650)
        self.days_spin.setSuffix(" 日")
        row.addWidget(self.days_spin)
        self.apply_days_button = make_button("適用")
        self.apply_days_button.clicked.connect(self._apply_days)
        row.addWidget(self.apply_days_button)
        row.addStretch(1)
        card.add(row)
        self.days_status = label("", "Small", wrap=True)
        card.add(self.days_status)
        card.add(label(
            "残るのは「相手のPCから届いた変更」でこのPCのファイルが上書き・削除されたときの、直前の版です。"
            "このPCで自分が上書き・削除したファイルの直前の版は、相手のPCの側に残ります。"
            "古い版ほど間引かれ（1日以内は1時間ごと、30日以内は1日ごと）、日数を過ぎると消えます。"
            "過去の版は同期フォルダの中の .stversions に置かれ、その分のディスク容量を使います。",
            "Small", wrap=True))
        outer.addWidget(card)

        # --- 大量削除 ---
        card = Card("大量削除の検知")
        self.guard_check = QCheckBox("大量の削除を検知したら、同期を一時停止して知らせる")
        self.guard_check.toggled.connect(self._on_guard)
        card.add(self.guard_check)
        row = QHBoxLayout()
        row.addWidget(label("一度に削除される件数がこの数以上なら止める"))
        self.guard_spin = QSpinBox()
        self.guard_spin.setRange(1, 1_000_000)
        self.guard_spin.setSuffix(" 件")
        self.guard_spin.editingFinished.connect(self._on_guard)
        row.addWidget(self.guard_spin)
        row.addStretch(1)
        card.add(row)
        self.guard_note = label(GUARD_HONEST_TEXT, "Small", wrap=True)
        card.add(self.guard_note)
        outer.addWidget(card)

        # --- 解除 ---
        card = Card("同期設定の解除")
        card.add(label("このPCの同期設定を解除します。同期フォルダの中のファイルは、このPCにも相手のPCにも残ります。",
                       "Sub", wrap=True))
        row = QHBoxLayout()
        self.reset_button = make_button("同期設定を解除…", danger=True)
        self.reset_button.clicked.connect(self._reset)
        row.addWidget(self.reset_button)
        row.addStretch(1)
        card.add(row)
        outer.addWidget(card)

        # --- このアプリについて ---
        card = Card("このアプリについて")
        self.about_label = label("", wrap=True, selectable=True)
        card.add(self.about_label)
        card.add(label(UNITY_NOTE, "Small", wrap=True))
        outer.addWidget(card)

        self.message = label("", "ErrorText", wrap=True)
        outer.addWidget(self.message)
        outer.addStretch(1)

        if controller is not None:
            controller.actionFinished.connect(self._on_action)
        self.apply_settings(self.settings)
        self.set_engine_version(None)

    # ---- 表示 ----
    def set_guard_note(self, text: str) -> None:
        self.guard_note.setText(text)

    def set_engine_version(self, version: str | None) -> None:
        st = version or SUPPORTED_SYNCTHING
        self.about_label.setText(f"{APP_NAME} {__version__}\n" + ABOUT_TEXT.format(st=st))

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self._loading = True
        try:
            index = max(0, self.theme_combo.findData(settings.theme))
            self.theme_combo.setCurrentIndex(index)
            self.minimized_check.setChecked(bool(settings.start_minimized))
            for key, check in self.preset_checks.items():
                check.setChecked(key in (settings.ignore_presets or []))
            self.custom_edit.setPlainText("\n".join(settings.custom_ignores or []))
            self.days_spin.setValue(int(settings.versioning_days))
            self.guard_check.setChecked(bool(settings.mass_delete_guard_enabled))
            self.guard_spin.setValue(int(settings.mass_delete_threshold))
            configured = bool(settings.folder_id)
            self.apply_ignores_button.setEnabled(configured)
            self.reset_button.setEnabled(bool(settings.folder_id or settings.peer_device_id))
            self._refresh_autostart()
        finally:
            self._loading = False

    def _refresh_autostart(self) -> None:
        enabled = self.autostart.is_enabled()
        was_loading, self._loading = self._loading, True
        self.autostart_check.setChecked(enabled)
        self._loading = was_loading
        if not self.autostart.can_enable:
            self.autostart_check.setEnabled(enabled)  # 開発中は「登録」できない。残っている登録の解除だけ可
            self.autostart_note.setText("開発中の実行では自動起動を登録できません（インストールした FolderLink で設定できます）。")
        elif enabled and not self.autostart.points_to_this_executable():
            self.autostart_note.setText("別の場所の FolderLink が自動起動に登録されています。オフ→オンで、この FolderLink に登録し直せます。")
        else:
            self.autostart_note.setText("")
        self.autostart_note.setVisible(bool(self.autostart_note.text()))

    def on_shown(self) -> None:
        self.message.setText("")
        self.ignores_status.setText("")
        self.days_status.setText("")
        self._refresh_autostart()
        if self.controller is not None:
            self.controller.run("settings:version", lambda s: s.engine_version())

    # ---- 操作 ----
    def _save(self, **changes) -> None:
        for key, value in changes.items():
            setattr(self.settings, key, value)
        if self.controller is not None:
            self.controller.run("settings:save", lambda s: s.save_settings(**changes))

    def _on_theme(self) -> None:
        if self._loading:
            return
        key = self.theme_combo.currentData()
        self._save(theme=key)
        self.themeChanged.emit(key)

    def _on_minimized(self, checked: bool) -> None:
        if not self._loading:
            self._save(start_minimized=bool(checked))

    def _on_autostart(self, checked: bool) -> None:
        if self._loading:
            return
        try:
            self.autostart.set_enabled(bool(checked))
            self.message.setText("")
        except AutoStartError as exc:
            self.message.setText(str(exc))
        self._refresh_autostart()

    def _on_guard(self, *_args) -> None:
        if self._loading:
            return
        self._save(mass_delete_guard_enabled=self.guard_check.isChecked(),
                   mass_delete_threshold=int(self.guard_spin.value()))

    def selected_presets(self) -> list[str]:
        return [key for key, check in self.preset_checks.items() if check.isChecked()]

    def custom_lines(self) -> list[str]:
        return ignore_presets.clean_custom_lines(self.custom_edit.toPlainText().splitlines())

    def _apply_ignores(self) -> None:
        if self.controller is None:
            return
        presets, custom = self.selected_presets(), self.custom_lines()
        self.ignores_status.setText("適用しています…")
        self.controller.run("settings:ignores", lambda s: s.update_ignores(presets, custom))

    def _apply_days(self) -> None:
        if self.controller is None:
            return
        days = int(self.days_spin.value())
        self.days_status.setText("適用しています…")
        self.controller.run("settings:days", lambda s: s.set_versioning_days(days))

    def _reset(self) -> None:
        if not self._confirm(self, "同期設定の解除",
                             "このPCの同期設定を解除します。\n\n"
                             "・同期フォルダの中のファイルは残ります（消えません）。\n"
                             "・相手のPCのファイルも残ります。\n"
                             "・もう一度同期するには、初期設定をやり直します。\n\n続けますか？"):
            return
        if self.controller is not None:
            self.controller.run("settings:reset", lambda s: s.reset_sync(forget_peer=True))

    def _on_action(self, name: str, ok: bool, result) -> None:
        if not name.startswith("settings:"):
            return
        if name == "settings:version":
            if ok:
                self.set_engine_version(result)
            return
        if not ok:
            self.message.setText(str(result))
            self.ignores_status.setText("")
            self.days_status.setText("")
            return
        self.message.setText("")
        if name == "settings:ignores":
            self.ignores_status.setText("適用しました。相手のPCでも同じ設定にしてください。")
        elif name == "settings:days":
            self.days_status.setText("適用しました。")
        elif name == "settings:reset":
            self.resetRequested.emit()
        if self.controller is not None and name in ("settings:save", "settings:ignores", "settings:days",
                                                    "settings:reset"):
            self.settingsChanged.emit(self.controller.service.settings)
