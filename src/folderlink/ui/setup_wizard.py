"""初期設定ウィザード（6ステップ）。

    ① フォルダ選択 → ② このPCのデバイス ID → ③ 相手の ID を入力 → ④ 双方で承認
    → ⑤ 初回同期前プレビュー → ⑥ 同期開始

- ID はコピー・貼り付けに対応。入力の誤りは、その場で理由を表示する。
- ⑤ では、フォルダを「送信のみ」で登録して、相手と一覧だけを交換する。この間、このPCの
  ファイルは一切書き換えられない（結合テストで確認済み）。数字が取れないうちは「—」。
- エンジンへの操作はすべて EngineController 経由（別スレッド）。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from folderlink.core import formatting as fm
from folderlink.core import ignore_presets
from folderlink.core.config_store import Settings
from folderlink.core.engine_service import PreviewInfo
from folderlink.core.pairing import DeviceIdError, normalize_device_id, short_device_id
from folderlink.ui import theme
from folderlink.ui.widgets import Banner, Card, label, make_button, repolish

STEP_TITLES = ["フォルダ選択", "このPCの ID", "相手の ID", "双方で承認", "同期前の確認", "同期開始"]
_CIRCLED = "①②③④⑤⑥"


def format_id_two_lines(device_id: str) -> str:
    """56 文字の ID を、読みやすいよう 2 行に分ける。"""
    groups = (device_id or "").split("-")
    if len(groups) != 8:
        return device_id or ""
    return "-".join(groups[:4]) + "-\n" + "-".join(groups[4:])


class SetupWizard(QWidget):
    finished = Signal()          # 同期を開始して、ウィザードを閉じる
    cancelled = Signal()         # 途中でやめる

    def __init__(self, palette: theme.Palette, controller=None, settings: Settings | None = None,
                 choose_folder: Callable[[QWidget, str], str] | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self._palette = palette
        self.controller = controller
        self.settings = settings or Settings()
        self._choose_folder = choose_folder or self._default_choose_folder
        self.my_id: str = getattr(controller, "device_id", None) or ""
        self.peer_id: str = ""
        self._folder_ok = False
        self._peer_registered = False
        self._peer_connected = False
        self._preview_ready = False
        self._busy = False
        self._started = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        # --- ステップの表示 ---
        steps_row = QHBoxLayout()
        steps_row.setSpacing(14)
        self.step_labels = []
        for i, title in enumerate(STEP_TITLES):
            lab = label(f"{_CIRCLED[i]} {title}", "StepTodo")
            self.step_labels.append(lab)
            steps_row.addWidget(lab)
        steps_row.addStretch(1)
        outer.addLayout(steps_row)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        for build in (self._build_folder, self._build_my_id, self._build_peer_id, self._build_approve,
                      self._build_preview, self._build_start):
            self.stack.addWidget(build())

        # --- 下のボタン ---
        self.message = label("", "ErrorText", wrap=True)
        outer.addWidget(self.message)
        nav = QHBoxLayout()
        self.cancel_button = make_button("やめる", flat=True)
        self.cancel_button.clicked.connect(self._on_cancel)
        self.back_button = make_button("戻る")
        self.back_button.clicked.connect(self.go_back)
        self.next_button = make_button("次へ", accent=True)
        self.next_button.clicked.connect(self.go_next)
        nav.addWidget(self.cancel_button)
        nav.addStretch(1)
        nav.addWidget(self.back_button)
        nav.addWidget(self.next_button)
        outer.addLayout(nav)

        self._poll = QTimer(self)
        self._poll.setInterval(2000)
        self._poll.timeout.connect(self._poll_tick)

        if controller is not None:
            controller.engineStarted.connect(self.set_my_id)
            controller.actionFinished.connect(self._on_action)
        self.set_my_id(self.my_id)
        self.load_from_settings(self.settings)
        self._show_step(0)

    # ------------------------------------------------------------ 各ステップの画面
    def _build_folder(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        card = Card("同期するフォルダ")
        card.add(label("このPCで同期したいフォルダを選んでください。相手のPCでは、別の場所のフォルダを選べます。",
                       "Sub", wrap=True))
        row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText(r"例: C:\Users\あなたの名前\Desktop\共有フォルダ")
        self.folder_edit.textChanged.connect(self._on_folder_text)
        self.browse_button = make_button("参照…")
        self.browse_button.clicked.connect(self._on_browse)
        row.addWidget(self.folder_edit, 1)
        row.addWidget(self.browse_button)
        card.add(row)
        self.folder_status = label("", "Small", wrap=True)
        card.add(self.folder_status)
        lay.addWidget(card)

        card = Card("同期しないもの（除外）")
        card.add(label("作り直せるファイルや PC ごとに違うファイルは、同期しないほうが安全で速くなります。"
                       "あとから「設定」で変えられます。", "Sub", wrap=True))
        self.preset_checks: dict[str, QCheckBox] = {}
        for key, preset in ignore_presets.PRESETS.items():
            check = QCheckBox(preset.name)
            check.setToolTip(preset.description)
            self.preset_checks[key] = check
            card.add(check)
            card.add(label(preset.description, "Small", wrap=True))
        self.unity_hint = label("", "Small", wrap=True)
        card.add(self.unity_hint)
        lay.addWidget(card)
        lay.addStretch(1)
        return page

    def _build_my_id(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        card = Card("このPCのデバイス ID")
        card.add(label("この ID を、相手のPCの FolderLink に入力してもらいます。「コピー」を押して、"
                       "メールやチャットで相手に送ってください。", "Sub", wrap=True))
        self.my_id_label = label("準備中…", "Mono", selectable=True)
        card.add(self.my_id_label)
        row = QHBoxLayout()
        self.copy_button = make_button("コピー", accent=True)
        self.copy_button.clicked.connect(self._on_copy)
        self.copy_status = label("", "OkText")
        row.addWidget(self.copy_button)
        row.addWidget(self.copy_status)
        row.addStretch(1)
        card.add(row)
        card.add(label("この ID は秘密ではありません。知られても、あなたが相手の ID を登録しない限り接続されません。",
                       "Small", wrap=True))
        lay.addWidget(card)
        lay.addStretch(1)
        return page

    def _build_peer_id(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        card = Card("相手のPCのデバイス ID")
        card.add(label("相手のPCの FolderLink に表示されている ID を貼り付けてください。", "Sub", wrap=True))
        self.peer_edit = QPlainTextEdit()
        self.peer_edit.setObjectName("PeerIdEdit")
        self.peer_edit.setPlaceholderText("XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX")
        self.peer_edit.setFixedHeight(64)
        self.peer_edit.setTabChangesFocus(True)
        self.peer_edit.textChanged.connect(self._on_peer_text)
        card.add(self.peer_edit)
        row = QHBoxLayout()
        self.paste_button = make_button("貼り付け")
        self.paste_button.clicked.connect(self._on_paste)
        self.peer_status = label("", "Small", wrap=True)
        row.addWidget(self.paste_button)
        row.addWidget(self.peer_status, 1)
        card.add(row)
        grid = QGridLayout()
        grid.addWidget(label("相手のPCの呼び名（自由）"), 0, 0)
        self.peer_name_edit = QLineEdit()
        self.peer_name_edit.setPlaceholderText("例: 自宅のPC")
        self.peer_name_edit.setMaxLength(40)
        grid.addWidget(self.peer_name_edit, 0, 1)
        grid.setColumnStretch(1, 1)
        card.add(grid)
        lay.addWidget(card)

        self.pending_card = Card("接続を求めているPC")
        self.pending_label = label("", "Sub", wrap=True)
        self.pending_card.add(self.pending_label)
        self.pending_use_button = make_button("この ID を使う")
        self.pending_use_button.clicked.connect(self._on_use_pending)
        row = QHBoxLayout()
        row.addWidget(self.pending_use_button)
        row.addStretch(1)
        self.pending_card.add(row)
        self.pending_card.hide()
        self._pending_id = ""
        lay.addWidget(self.pending_card)
        lay.addStretch(1)
        return page

    def _build_approve(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        card = Card("双方で承認")
        self.approve_text = label("", wrap=True)
        card.add(self.approve_text)
        grid = QGridLayout()
        grid.setVerticalSpacing(8)
        grid.addWidget(label("このPCでの登録"), 0, 0)
        self.approve_local = label("—")
        grid.addWidget(self.approve_local, 0, 1)
        grid.addWidget(label("相手のPCとの接続"), 1, 0)
        self.approve_remote = label("—")
        grid.addWidget(self.approve_remote, 1, 1)
        grid.setColumnStretch(1, 1)
        card.add(grid)
        card.add(label("接続には、相手のPCでも FolderLink が起動していて、このPCの ID が入力されている必要があります。"
                       "インターネット越しの場合、最初の接続に 1〜2 分かかることがあります。", "Small", wrap=True))
        lay.addWidget(card)
        lay.addStretch(1)
        return page

    def _build_preview(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.preview_banner = Banner("info")
        lay.addWidget(self.preview_banner)
        card = Card("同期を始める前の確認")
        grid = QGridLayout()
        grid.setVerticalSpacing(8)
        grid.setHorizontalSpacing(18)
        rows = [("このPCのフォルダにあるもの", "pv_local"), ("相手のPCから受け取るもの", "pv_receive"),
                ("相手のPCへ送るもの", "pv_send"), ("このPCで、相手の版に置き換わり得るファイル", "pv_differ")]
        for i, (title, attr) in enumerate(rows):
            grid.addWidget(label(title), i, 0)
            value = label("—", "MetricValue")
            setattr(self, attr, value)
            grid.addWidget(value, i, 1)
        grid.setColumnStretch(1, 1)
        card.add(grid)
        self.preview_note = label("", "Small", wrap=True)
        card.add(self.preview_note)
        lay.addWidget(card)

        card = Card("同期を始めると起きること")
        card.add(label(
            "・初回の同期では、どちらのPCのファイルも削除しません。両方のフォルダの中身を合わせます。\n"
            "・同じ名前で内容が違うファイルは、新しいほうが元の名前で残り、古いほうは"
            "「.sync-conflict-…」の付いた別名（競合コピー）で残ります。あとで「履歴」から選べます。\n"
            "・同期を始めたあとは、片方で消したファイルは、もう片方からも消えます"
            "（消える前の版は 30 日ほど「履歴」に残ります）。\n"
            "・同期はバックアップではありません。大切なデータは別の場所にもバックアップしてください。",
            wrap=True))
        lay.addWidget(card)
        lay.addStretch(1)
        return page

    def _build_start(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        card = Card("同期開始")
        self.summary_label = label("", wrap=True, selectable=True)
        card.add(self.summary_label)
        self.confirm_check = QCheckBox("上の内容と「同期を始めると起きること」を確認しました")
        self.confirm_check.toggled.connect(lambda _on: self._update_nav())
        card.add(self.confirm_check)
        self.start_status = label("", "Sub", wrap=True)
        card.add(self.start_status)
        lay.addWidget(card)
        lay.addStretch(1)
        return page

    # ------------------------------------------------------------ 外からの設定
    def load_from_settings(self, settings: Settings) -> None:
        self.settings = settings
        if settings.folder_path:
            self.folder_edit.setText(settings.folder_path)
        for key, check in self.preset_checks.items():
            check.setChecked(key in (settings.ignore_presets or []))
        if settings.peer_device_id:
            self.peer_edit.setPlainText(settings.peer_device_id)
            self.peer_name_edit.setText(settings.peer_name or "")

    def restart(self, settings: Settings) -> None:
        """最初のステップからやり直す。"""
        self._peer_registered = self._peer_connected = self._preview_ready = False
        self._started = False
        self._busy = False
        self.confirm_check.setEnabled(True)
        self.confirm_check.setChecked(False)
        self.load_from_settings(settings)
        self._show_step(0)

    def set_my_id(self, device_id: str) -> None:
        self.my_id = device_id or ""
        if self.my_id:
            self.my_id_label.setText(format_id_two_lines(self.my_id))
            self.copy_button.setEnabled(True)
        else:
            self.my_id_label.setText("準備中…（同期エンジンを起動しています）")
            self.copy_button.setEnabled(False)
        self._on_peer_text()
        self._update_nav()

    def on_shown(self) -> None:
        if self.current_step() in (2, 3, 4):
            self._poll.start()

    def on_hidden(self) -> None:
        self._poll.stop()

    @property
    def started(self) -> bool:
        return self._started

    # ------------------------------------------------------------ ステップの移動
    def current_step(self) -> int:
        return self.stack.currentIndex()

    def _show_step(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for i, lab in enumerate(self.step_labels):
            lab.setObjectName("StepActive" if i == index else ("StepDone" if i < index else "StepTodo"))
            repolish(lab)
        self.message.setText("")
        self.copy_status.setText("")
        if index in (2, 3, 4):
            self._poll.start()
            self._poll_tick()
        else:
            self._poll.stop()
        if index == 3:
            self._enter_approve()
        elif index == 4:
            self._enter_preview()
        elif index == 5:
            self._enter_start()
        self._update_nav()

    def _update_nav(self) -> None:
        step = self.current_step()
        self.back_button.setEnabled(step > 0 and not self._busy and not self._started)
        self.cancel_button.setEnabled(not self._busy)
        text = "次へ"
        enabled = not self._busy
        if step == 0:
            enabled = enabled and self._folder_ok
        elif step == 1:
            enabled = enabled and bool(self.my_id)
        elif step == 2:
            enabled = enabled and bool(self.peer_id) and bool(self.my_id)
        elif step == 3:
            # 登録が済めば進める（相手がまだ起動していなくても、あとから繋がる）
            enabled = enabled and self._peer_registered
            if not self._peer_connected:
                text = "接続を待たずに次へ"
        elif step == 4:
            enabled = enabled and self._preview_ready
        elif step == 5:
            text = "ダッシュボードへ" if self._started else "同期を開始"
            enabled = enabled and (self._started or self.confirm_check.isChecked())
        self.next_button.setText(text)
        self.next_button.setEnabled(bool(enabled))

    def go_back(self) -> None:
        if self.current_step() > 0 and not self._busy:
            self._show_step(self.current_step() - 1)

    def go_next(self) -> None:
        step = self.current_step()
        if self._busy:
            return
        if step == 0:
            self._run("check_folder", lambda s, p=self.folder_path(): str(s.check_folder(p)))
        elif step == 5:
            if self._started:
                self.finished.emit()
            else:
                self._do_start()
        elif step < 5:
            self._show_step(step + 1)

    def _on_cancel(self) -> None:
        self._poll.stop()
        self.cancelled.emit()

    # ------------------------------------------------------------ ① フォルダ
    @staticmethod
    def _default_choose_folder(parent: QWidget, start: str) -> str:
        return QFileDialog.getExistingDirectory(parent, "同期するフォルダを選ぶ", start)

    def folder_path(self) -> str:
        return self.folder_edit.text().strip().strip('"')

    def selected_presets(self) -> list[str]:
        return [key for key, check in self.preset_checks.items() if check.isChecked()]

    def _on_browse(self) -> None:
        chosen = self._choose_folder(self, self.folder_path())
        if chosen:
            self.folder_edit.setText(chosen.replace("/", "\\"))

    def _on_folder_text(self) -> None:
        path = self.folder_path()
        self._folder_ok = bool(path)
        self.folder_status.setText("")
        self.unity_hint.setText("")
        self.folder_edit.setProperty("invalid", False)
        repolish(self.folder_edit)
        if path:
            # Unity プロジェクトかどうかは、フォルダを少し覗くだけなのでここで見る
            try:
                projects = ignore_presets.detect_unity_projects(path, max_depth=2, limit=5)
            except OSError:
                projects = []
            if projects:
                where = "このフォルダ" if projects == [""] else "、".join(p for p in projects)
                self.unity_hint.setText(f"Unity プロジェクトが見つかりました（{where}）。「Unity プロジェクト」の除外をおすすめします。")
                self.preset_checks["unity"].setChecked(True)
        self._update_nav()

    # ------------------------------------------------------------ ② 自分の ID
    def _on_copy(self) -> None:
        if not self.my_id:
            return
        QGuiApplication.clipboard().setText(self.my_id)
        self.copy_status.setText("コピーしました")

    # ------------------------------------------------------------ ③ 相手の ID
    def _on_paste(self) -> None:
        text = QGuiApplication.clipboard().text()
        if text:
            self.peer_edit.setPlainText(text.strip())

    def _on_peer_text(self) -> None:
        text = self.peer_edit.toPlainText()
        self.peer_id = ""
        invalid = False
        if not text.strip():
            self.peer_status.setText("")
        else:
            try:
                device_id = normalize_device_id(text)
                if self.my_id and device_id == self.my_id:
                    raise DeviceIdError(
                        "これはこのPC自身の ID です。相手のPCに表示されている ID を入力してください。", "self")
                self.peer_id = device_id
                self.peer_status.setObjectName("OkText")
                self.peer_status.setText("正しい形式の ID です。")
            except DeviceIdError as exc:
                invalid = True
                self.peer_status.setObjectName("ErrorText")
                self.peer_status.setText(str(exc))
            repolish(self.peer_status)
        self.peer_edit.setProperty("invalid", invalid)
        repolish(self.peer_edit)
        self._peer_registered = False
        self._peer_connected = False
        self._update_nav()

    def _on_use_pending(self) -> None:
        if self._pending_id:
            self.peer_edit.setPlainText(self._pending_id)

    def _show_pending(self, pending: list) -> None:
        candidates = [p for p in pending if p.device_id != self.my_id]
        if not candidates or self.current_step() != 2:
            self.pending_card.hide()
            return
        first = candidates[0]
        self._pending_id = first.device_id
        name = f"「{first.name}」" if first.name else ""
        self.pending_label.setText(
            f"次のPC{name}が、このPCへの接続を求めています。相手に ID を確かめてから使ってください。\n{first.device_id}")
        self.pending_card.show()

    # ------------------------------------------------------------ ④ 承認
    def _enter_approve(self) -> None:
        name = self.peer_name_edit.text().strip() or short_device_id(self.peer_id)
        self.approve_text.setText(
            f"相手のPC（{name}）を、このPCに登録します。相手のPCでも、このPCの ID を入力して同じ画面まで進めてください。")
        self.approve_local.setText("登録しています…")
        self.approve_remote.setText("—")
        self._peer_registered = False
        self._peer_connected = False
        peer_id, peer_name = self.peer_id, self.peer_name_edit.text().strip()
        self._run("register_peer", lambda s: s.register_peer(peer_id, peer_name))

    # ------------------------------------------------------------ ⑤ プレビュー
    def _enter_preview(self) -> None:
        self._preview_ready = False
        self.preview_banner.set_kind("info")
        self.preview_banner.set_text("フォルダの中身を調べています。この確認の間、どちらのPCのファイルも変更されません。")
        for attr in ("pv_local", "pv_receive", "pv_send", "pv_differ"):
            getattr(self, attr).setText("—")
        self.preview_note.setText("")
        path, presets = self.folder_path(), self.selected_presets()
        custom = list(self.settings.custom_ignores or [])
        self._run("prepare_preview", lambda s: s.prepare_preview(path, presets, custom))

    def apply_preview(self, info: PreviewInfo) -> None:
        def pair(items: int | None, size: int | None) -> str:
            if items is None:
                return "—"
            return f"{fm.fmt_count(items)} ・ {fm.fmt_bytes(size)}"

        self.pv_local.setText("調べています…" if info.scanning and info.local_files is None
                              else pair(info.local_files, info.local_bytes))
        self.pv_receive.setText(pair(info.receive_items, info.receive_bytes))
        self.pv_send.setText(pair(info.send_items, info.send_bytes))
        if info.differing is None:
            self.pv_differ.setText("—")
        else:
            more = " 以上" if info.differing_is_partial else ""
            self.pv_differ.setText(f"{fm.fmt_count(info.differing)}{more}")
        self._preview_ready = info.local_files is not None and not info.scanning
        if info.peer_connected is False:
            self.preview_banner.set_kind("warn")
            self.preview_banner.set_text(
                "相手のPCに接続できていません。相手側の数字は表示できません（—）。"
                "このまま進めることもできますが、接続してから確認することをおすすめします。")
            self.preview_note.setText("")
        elif not info.remote_ready:
            self.preview_banner.set_kind("info")
            self.preview_banner.set_text(
                "相手のPCがこの画面まで進むと、相手側の数字が表示されます。まだ届いていない数字は「—」です。")
            self.preview_note.setText("")
        elif info.both_have_files:
            self.preview_banner.set_kind("warn")
            self.preview_banner.set_text(
                "注意: 両方のフォルダに、すでにファイルがあります。同期を始めると、両方の中身が合わさります。"
                "同じ名前で内容が違うファイルは、古いほうが競合コピー（別名）として残ります。")
            self.preview_note.setText(
                "「置き換わり得るファイル」は、このPCにも相手のPCにも同じ名前であり、相手の版のほうが新しいものです。"
                "内容が同じなら何も起きません。内容が違えば、このPCの版は競合コピーとして残ります（消えません）。")
        else:
            self.preview_banner.set_kind("info")
            self.preview_banner.set_text("片方のフォルダだけにファイルがあります。そのまま反対側へコピーされます。")
            self.preview_note.setText("")
        self._update_nav()

    # ------------------------------------------------------------ ⑥ 開始
    def _enter_start(self) -> None:
        name = self.peer_name_edit.text().strip() or short_device_id(self.peer_id)
        presets = [ignore_presets.PRESETS[k].name for k in self.selected_presets()] or ["なし"]
        self.summary_label.setText(
            f"同期するフォルダ: {self.folder_path()}\n"
            f"相手のPC: {name}（{short_device_id(self.peer_id)}…）\n"
            f"除外: {'、'.join(presets)}\n"
            f"過去の版の保持: 約 {self.settings.versioning_days} 日")
        self.start_status.setText("")

    def _do_start(self) -> None:
        path, presets = self.folder_path(), self.selected_presets()
        custom = list(self.settings.custom_ignores or [])
        self.start_status.setText("同期を開始しています…")
        self._run("start_sync", lambda s: s.start_sync(path, presets=presets, custom_ignores=custom))

    # ------------------------------------------------------------ エンジンとのやり取り
    def _run(self, name: str, fn) -> None:
        if self.controller is None:
            return
        self._busy = True
        self.message.setText("")
        self._update_nav()
        self.controller.run("wizard:" + name, fn)

    def _poll_tick(self) -> None:
        if self.controller is None or self._busy:
            return
        step = self.current_step()
        if step == 2:
            self.controller.run("wizard:poll_pending", lambda s: s.pending_devices())
        elif step == 3 and self._peer_registered:
            self.controller.run("wizard:poll_peer", lambda s: s.peer_state())
        elif step == 4:
            self.controller.run("wizard:poll_preview", lambda s: s.preview_info())

    def _on_action(self, name: str, ok: bool, result) -> None:
        if not name.startswith("wizard:"):
            return
        name = name.split(":", 1)[1]
        if name.startswith("poll_"):
            if not ok:
                return  # 定期確認の失敗は、次の回でやり直す
            if name == "poll_pending":
                self._show_pending(result or [])
            elif name == "poll_peer" and result is not None:
                self._apply_peer_state(result)
            elif name == "poll_preview" and self.current_step() == 4:
                self.apply_preview(result)
            return
        self._busy = False
        if not ok:
            self.message.setText(str(result))
            if name == "check_folder":
                self.folder_edit.setProperty("invalid", True)
                repolish(self.folder_edit)
            elif name == "register_peer":
                self.approve_local.setText("登録できませんでした")
            elif name == "start_sync":
                self.start_status.setText("")
            self._update_nav()
            return
        if name == "check_folder":
            self.folder_edit.setText(str(result))
            self._folder_ok = True
            self._busy = False
            self._show_step(1)
            return
        if name == "register_peer":
            self.peer_id = str(result)
            self._peer_registered = True
            self.approve_local.setText("登録しました")
            self.approve_remote.setText("相手のPCからの接続を待っています…")
            self._poll_tick()
        elif name == "prepare_preview":
            self._poll_tick()
        elif name == "start_sync":
            self._started = True
            self._poll.stop()
            self.start_status.setText("同期を開始しました。進み具合はダッシュボードで確認できます。")
            self.confirm_check.setEnabled(False)
        self._update_nav()

    def _apply_peer_state(self, state) -> None:
        self._peer_connected = bool(state.connected)
        if state.connected:
            route = "（中継サーバー経由）" if state.via_relay else "（直接接続）" if state.via_relay is False else ""
            self.approve_remote.setObjectName("OkText")
            self.approve_remote.setText(f"接続しました {route}")
        else:
            self.approve_remote.setObjectName("")
            self.approve_remote.setText("相手のPCからの接続を待っています…")
        repolish(self.approve_remote)
        self._update_nav()
