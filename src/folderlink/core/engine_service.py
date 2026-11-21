"""画面から見た「同期エンジン一式」。Qt に依存しない。

設定の保存（ConfigStore）、プロセス管理（SyncthingManager）、ペアリング（Pairing）、
状態の取得（StatusCollector）を1つにまとめる。**どのメソッドも時間がかかり得るので、
画面のスレッドからは呼ばず、ワーカースレッド（ui/engine_controller.py）から呼ぶ。**
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from folderlink.core import ignore_presets
from folderlink.core.api_client import ApiError
from folderlink.core.config_store import ConfigStore, Settings
from folderlink.core.conflicts import ConflictCounter
from folderlink.core.pairing import (
    FolderContents,
    Pairing,
    PairingError,
    PeerState,
    PendingDevice,
    PendingFolder,
    derive_folder_id,
    scan_folder_contents,
    validate_sync_folder,
)
from folderlink.core.sync_status import StatusCollector, StatusSnapshot, SyncState
from folderlink.core.syncthing_manager import EngineError, SyncthingManager

log = logging.getLogger(__name__)

PREVIEW_NEED_LIMIT = 2000


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


@dataclass
class PreviewInfo:
    """初回同期前プレビューの数字。None は「まだ分からない」（画面では「—」）。"""

    local_files: int | None = None       # このPCのフォルダにあるファイル数
    local_bytes: int | None = None
    global_files: int | None = None      # 両方を合わせたときのファイル数
    peer_connected: bool | None = None
    remote_ready: bool = False           # 相手もこのフォルダを登録していて、一覧を交換できている
    scanning: bool = False               # このPCのフォルダをまだ調べている途中
    receive_items: int | None = None     # 相手から受け取る項目（相手にだけある・相手のほうが新しい）
    receive_bytes: int | None = None
    send_items: int | None = None        # 相手へ送る項目
    send_bytes: int | None = None
    differing: int | None = None         # 同じ名前で、両方にあって内容が違うファイル
    differing_is_partial: bool = False   # 件数が多く、differing は一部だけ数えた値

    @property
    def both_have_files(self) -> bool | None:
        """両方のフォルダにファイルがあるか（分からなければ None）。"""
        if not self.remote_ready or self.local_files is None or self.global_files is None:
            return None
        if self.local_files == 0:
            return False
        # 自分が持っていて相手に無いものを除いても、相手側に何かあるか
        remote_has = (self.global_files - (self.send_items or 0)) > 0 or bool(self.receive_items)
        return remote_has


@dataclass
class HistoryData:
    conflicts: list            # list[ConflictFile]
    versions: list             # list[VersionEntry]
    versions_bytes: int
    versions_files: int
    versions_error: str | None = None


class EngineService:
    def __init__(
        self,
        store: ConfigStore,
        *,
        binary: Path | str | None = None,
        isolated: bool = False,
        listen_address: str | None = None,
        manager_factory: Callable[..., SyncthingManager] = SyncthingManager,
        guard_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.store = store
        self.settings: Settings = store.load_settings()
        self._binary = binary
        self._isolated = isolated
        self._listen_address = listen_address
        self._manager_factory = manager_factory
        self._lock = threading.RLock()
        self.manager: SyncthingManager | None = None
        self.pairing: Pairing | None = None
        self._collector: StatusCollector | None = None
        self._conflicts: ConflictCounter | None = None
        self.device_id: str | None = None
        self.last_error: str | None = None
        self._was_running = False
        self._guard_factory = guard_factory
        self._guard = None
        self.guard_trip = None   # safety_guard.GuardTrip（利用者の確認待ち）

    # ---- 起動・停止 ----
    def forbidden_paths(self) -> list[Path]:
        """同期フォルダにしてはいけない場所（FolderLink 自身の設定フォルダ）。"""
        return [self.store.base_dir]

    def start(self, cancel: threading.Event | None = None) -> str:
        """エンジンを起動し、本人確認まで済ませる。このPCのデバイス ID を返す。"""
        with self._lock:
            if self.manager is not None and self.manager.is_verified and self.manager.is_process_alive():
                return self.device_id or ""
            self.store.ensure_dirs()
            secrets = self.store.load_secrets()
            manager = self._manager_factory(
                self.store.syncthing_home,
                secrets.api_key,
                binary=self._binary,
                log_path=self.store.log_dir / "syncthing.log",
                store=self.store,
                isolated=self._isolated,
                listen_address=self._listen_address,
            )
            self.manager = manager
            try:
                self.device_id = manager.ensure_home()
                client = manager.start(cancel)
            except EngineError as exc:
                self.last_error = str(exc)
                raise
            self.last_error = None
            self._was_running = True
            self.pairing = Pairing(client, self.device_id)
            self._rebuild_collector()
            return self.device_id

    def stop(self) -> None:
        with self._lock:
            guard, self._guard = self._guard, None
            if guard is not None:
                guard.stop(timeout=2.0)
            manager, self.manager = self.manager, None
            self.pairing = None
            self._collector = None
            self._was_running = False
            if manager is not None:
                manager.stop()

    @property
    def is_running(self) -> bool:
        m = self.manager
        return m is not None and m.is_verified and m.is_process_alive()

    def has_crashed(self) -> bool:
        """起動に成功したあとで、エンジンのプロセスが落ちたか。"""
        m = self.manager
        return self._was_running and m is not None and not m.is_process_alive()

    def _client(self):
        if self.manager is None:
            raise EngineError("同期エンジンが起動していません。")
        return self.manager.client

    def _rebuild_collector(self) -> None:
        s = self.settings
        # 初期設定が終わるまで（プレビュー中を含む）は「未設定」として扱う
        folder_id = s.folder_id if s.setup_completed else None
        self._conflicts = ConflictCounter(s.folder_path) if s.folder_path and s.setup_completed else None
        self._collector = StatusCollector(
            self._client(), folder_id, s.peer_device_id, conflict_counter=self._conflicts
        ) if self.is_running else None
        if not s.setup_completed:
            self.guard_trip = None
        self._rebuild_guard()

    # ---- 状態 ----
    def snapshot(self) -> StatusSnapshot:
        """いまの状態。エンジンが動いていなくても例外にしない。"""
        now = datetime.now(timezone.utc)
        with self._lock:
            if self.has_crashed():
                code = self.manager.exit_code() if self.manager else None
                return StatusSnapshot(SyncState.ERROR, f"同期エンジンが停止しました（終了コード {code}）。", now)
            if not self.is_running or self._collector is None:
                if self.last_error:
                    return StatusSnapshot(SyncState.ERROR, self.last_error, now)
                return StatusSnapshot(SyncState.STARTING, "同期エンジンを起動しています…", now)
            collector = self._collector
        try:
            snap = collector.collect()
        except EngineError as exc:
            snap = StatusSnapshot(SyncState.ERROR, str(exc), now)
        snap.guard_trip = self.guard_trip
        return snap

    def invalidate_conflicts(self) -> None:
        if self._conflicts is not None:
            self._conflicts.invalidate()

    # ---- 一時停止 ----
    def is_paused(self) -> bool:
        s = self.settings
        if not s.folder_id:
            return False
        try:
            return bool(self._client().get_folder(s.folder_id).get("paused"))
        except (ApiError, EngineError):
            return False

    def set_paused(self, paused: bool) -> None:
        s = self.settings
        if not s.folder_id or self.pairing is None:
            raise PairingError("まだ同期の設定がありません。")
        self.pairing.set_folder_paused(s.folder_id, paused)

    # ---- 初期設定 ----
    def preview_folder(self, path: str) -> FolderContents:
        """初回同期前プレビュー用: 選んだフォルダに今あるものを数える（中身は変えない）。"""
        folder = validate_sync_folder(path, forbidden=self.forbidden_paths())
        return scan_folder_contents(folder)

    def check_folder(self, path: str) -> Path:
        return validate_sync_folder(path, forbidden=self.forbidden_paths())

    def check_peer_id(self, text: str) -> str:
        if self.pairing is None:
            raise PairingError("同期エンジンの準備ができていません。少し待ってからやり直してください。")
        return self.pairing.check_peer_id(text)

    def register_peer(self, peer_id: str, peer_name: str = "", addresses: list[str] | None = None) -> str:
        """相手を登録する（承認）。設定にも覚える。"""
        if self.pairing is None:
            raise PairingError("同期エンジンの準備ができていません。")
        peer_id = self.pairing.add_device(peer_id, peer_name, addresses)
        with self._lock:
            self.settings.peer_device_id = peer_id
            if peer_name.strip():
                self.settings.peer_name = peer_name.strip()
            self.store.save_settings(self.settings)
            self._rebuild_collector()
        return peer_id

    def pending_devices(self) -> list[PendingDevice]:
        return self.pairing.pending_devices() if self.pairing else []

    def pending_folders(self) -> list[PendingFolder]:
        return self.pairing.pending_folders() if self.pairing else []

    def peer_state(self) -> PeerState | None:
        if self.pairing is None or not self.settings.peer_device_id:
            return None
        return self.pairing.peer_state(self.settings.peer_device_id)

    def shared_folder_id(self) -> str:
        """2台で共通に使うフォルダ ID（2台のデバイス ID から決まる。どちらの PC でも同じ値）。"""
        if not self.device_id or not self.settings.peer_device_id:
            raise PairingError("先に相手のPCを登録してください。")
        return derive_folder_id(self.device_id, self.settings.peer_device_id)

    def prepare_preview(self, folder_path: str, presets: list[str] | None = None,
                        custom_ignores: list[str] | None = None) -> str:
        """初回同期前プレビューの準備。フォルダを「送信のみ」で登録する。

        「送信のみ」の間は、相手と一覧（どんなファイルがあるか）を交換するだけで、
        **このPCのファイルは一切書き換えられない**。両方の PC がこの状態なら、どちらのファイルも
        動かないまま、同期を始めたら何が起きるかを数えられる。`start_sync()` で双方向同期に切り替える。
        """
        return self._share(folder_path, presets, custom_ignores, folder_type="sendonly", completed=False)

    def preview_info(self) -> "PreviewInfo":
        """プレビューの数字。取れなかったものは None。"""
        s = self.settings
        info = PreviewInfo()
        if not s.folder_id or not s.peer_device_id or not s.folder_path:
            return info
        client = self._client()
        try:
            st = client.db_status(s.folder_id)
            info.scanning = str(st.get("state") or "") in ("scanning", "scan-waiting", "")
            info.local_files = _as_int(st.get("localFiles"))
            info.local_bytes = _as_int(st.get("localBytes"))
            info.receive_items = _as_int(st.get("needTotalItems"))
            info.receive_bytes = _as_int(st.get("needBytes"))
            info.global_files = _as_int(st.get("globalFiles"))
        except ApiError:
            pass
        try:
            conn = (client.connections().get("connections") or {}).get(s.peer_device_id) or {}
            info.peer_connected = bool(conn.get("connected"))
        except ApiError:
            pass
        if info.peer_connected:
            try:
                comp = client.db_completion(s.folder_id, s.peer_device_id)
                info.remote_ready = comp.get("remoteState") == "valid"
                if info.remote_ready:
                    items, deletes = _as_int(comp.get("needItems")), _as_int(comp.get("needDeletes"))
                    info.send_items = (items or 0) + (deletes or 0) if items is not None else None
                    info.send_bytes = _as_int(comp.get("needBytes"))
            except ApiError:
                pass
        if not info.remote_ready:
            # 相手の一覧をまだ受け取れていないので、「受け取るもの」は分からない
            info.receive_items = info.receive_bytes = info.send_items = info.send_bytes = None
            return info
        if info.receive_items:
            try:
                need = client.db_need(s.folder_id, page=1, perpage=PREVIEW_NEED_LIMIT)
                names = [str(f.get("name")) for key in ("progress", "queued", "rest")
                         for f in (need.get(key) or []) if isinstance(f, dict) and f.get("name")]
                root = Path(s.folder_path)
                info.differing = sum(1 for name in names if (root / name).is_file())
                info.differing_is_partial = info.receive_items > len(names)
            except (ApiError, OSError):
                info.differing = None
        else:
            info.differing = 0
        return info

    def start_sync(
        self,
        folder_path: str,
        *,
        folder_id: str | None = None,
        presets: list[str] | None = None,
        custom_ignores: list[str] | None = None,
        start_paused: bool = False,
    ) -> str:
        """フォルダを相手と共有して同期を始める。使ったフォルダ ID を返す。

        folder_id を省くと、2台のデバイス ID から決まる共通の ID を使う（両方の PC で同じ値になる）。
        除外の設定は、エンジンがフォルダを読み始める**前**に `.stignore` へ書く。
        """
        return self._share(folder_path, presets, custom_ignores, folder_type="sendreceive", completed=True,
                           folder_id=folder_id, paused=start_paused)

    def _share(self, folder_path: str, presets: list[str] | None, custom_ignores: list[str] | None, *,
               folder_type: str, completed: bool, folder_id: str | None = None, paused: bool = False) -> str:
        if self.pairing is None or not self.settings.peer_device_id:
            raise PairingError("先に相手のPCを登録してください。")
        folder = validate_sync_folder(folder_path, forbidden=self.forbidden_paths())
        fid = folder_id or self.shared_folder_id()
        presets = list(presets if presets is not None else self.settings.ignore_presets)
        custom = list(custom_ignores if custom_ignores is not None else self.settings.custom_ignores)
        ignore_presets.apply_to_folder(folder, presets, custom)
        self.pairing.share_folder(
            fid, folder, self.settings.peer_device_id,
            label=self.settings.folder_label or "FolderLink",
            versioning_days=self.settings.versioning_days,
            paused=paused,
            forbidden=self.forbidden_paths(),
            folder_type=folder_type,
        )
        with self._lock:
            self.settings.folder_path = str(folder)
            self.settings.folder_id = fid
            self.settings.ignore_presets = presets
            self.settings.custom_ignores = ignore_presets.clean_custom_lines(custom)
            self.settings.setup_completed = completed
            self.store.save_settings(self.settings)
            self._rebuild_collector()
        return fid

    def update_ignores(self, presets: list[str], custom_ignores: list[str]) -> None:
        """除外の設定を変える（動いているフォルダに、エンジン経由で反映）。"""
        s = self.settings
        if not s.folder_id or not s.folder_path:
            raise PairingError("まだ同期の設定がありません。")
        try:
            ignore_presets.apply_via_api(self._client(), s.folder_id, s.folder_path, presets, custom_ignores)
        except ApiError as exc:
            raise PairingError(f"除外の設定を反映できませんでした（{exc}）。") from None
        with self._lock:
            s.ignore_presets = list(presets)
            s.custom_ignores = ignore_presets.clean_custom_lines(custom_ignores)
            self.store.save_settings(s)

    def set_versioning_days(self, days: int) -> None:
        """過去の版を残す日数を変える（設定に保存し、動いているフォルダにも反映）。"""
        if not isinstance(days, int) or isinstance(days, bool) or not (1 <= days <= 3650):
            raise PairingError("保持する日数は 1〜3650 の間で指定してください。")
        s = self.settings
        if s.folder_id and self.manager is not None:
            from folderlink.core.pairing import staggered_versioning

            try:
                current = self._client().get_folder(s.folder_id).get("versioning") or {}
                merged = dict(current)
                merged.update(staggered_versioning(days))
                self._client().patch_folder(s.folder_id, {"versioning": merged})
            except ApiError as exc:
                raise PairingError(f"保持する日数を反映できませんでした（{exc}）。") from None
        with self._lock:
            s.versioning_days = days
            self.store.save_settings(s)

    def engine_version(self) -> str | None:
        return self.manager.version if self.manager is not None else None

    def reset_sync(self, forget_peer: bool = False) -> None:
        """同期の設定を解除する。**同期フォルダの中のファイルには一切触れない。**"""
        s = self.settings
        if self.pairing is not None:
            if s.folder_id:
                self.pairing.unshare_folder(s.folder_id)
            if forget_peer and s.peer_device_id:
                self.pairing.remove_device(s.peer_device_id)
        with self._lock:
            s.folder_id = None
            s.folder_path = None
            s.setup_completed = False
            if forget_peer:
                s.peer_device_id = None
                s.peer_name = ""
            self.store.save_settings(s)
            self._rebuild_collector()

    def save_settings(self, **changes: Any) -> Settings:
        with self._lock:
            for key, value in changes.items():
                if hasattr(self.settings, key):
                    setattr(self.settings, key, value)
            self.store.save_settings(self.settings)
            if {"mass_delete_guard_enabled", "mass_delete_threshold"} & set(changes):
                self._rebuild_guard()
            return self.settings

    # ---- 大量削除の検知 ----
    def _rebuild_guard(self) -> None:
        """設定に合わせて検知を始める・止める（_rebuild_collector から呼ばれる）。"""
        from folderlink.core.safety_guard import MassDeleteGuard

        old, self._guard = self._guard, None
        if old is not None:
            old.stop(timeout=0.5)
        s = self.settings
        if not (self.is_running and s.setup_completed and s.folder_id and s.mass_delete_guard_enabled):
            return
        guard = self._guard_factory(self._client(), s.folder_id, int(s.mass_delete_threshold),
                                    on_trip=self._on_guard_trip) if self._guard_factory else \
            MassDeleteGuard(self._client(), s.folder_id, int(s.mass_delete_threshold), on_trip=self._on_guard_trip)
        self._guard = guard
        guard.start()

    def _on_guard_trip(self, trip) -> None:
        self.guard_trip = trip

    def guard_accept(self) -> None:
        """「相手の削除を受け入れる」: 一時停止を解いて、残りの削除も適用させる。"""
        self.set_paused(False)
        self.guard_trip = None
        if self._guard is not None:
            self._guard.rearm()

    def guard_restore(self, wait_timeout_s: float = 600.0, sleep: Callable[[float], None] | None = None
                      ) -> dict[str, Any]:
        """「削除されたファイルを戻す」: 再開 → 届いた削除の適用が終わるのを待つ → 過去の版からまとめて戻す。

        戻したファイルは相手の PC にも同期される（＝相手での削除を取り消す）。
        """
        import time as _time

        from folderlink.core import history
        from folderlink.core.safety_guard import deleted_since, restore_deleted

        trip = self.guard_trip
        if trip is None:
            raise PairingError("確認が必要な大量削除はありません。")
        folder_id, folder_path = self._require_folder()
        sleep = sleep or _time.sleep
        self.set_paused(False)
        client = self._client()
        waited = 0.0
        while waited < wait_timeout_s:
            try:
                st = client.db_status(folder_id)
            except ApiError:
                st = {}
            if st.get("state") == "idle" and st.get("needDeletes") == 0:
                break
            sleep(1.0)
            waited += 1.0
        entries = deleted_since(history.list_versions(client, folder_id, folder_path), trip.first_delete_at)
        errors = restore_deleted(client, folder_id, entries)
        self.guard_trip = None
        if self._guard is not None:
            self._guard.rearm()
        self.invalidate_conflicts()
        return {"restored": len(entries) - len(errors), "failed": errors}

    # ---- 履歴（過去の版・競合） ----
    def _require_folder(self) -> tuple[str, str]:
        s = self.settings
        if not s.folder_id or not s.folder_path or not s.setup_completed:
            raise PairingError("まだ同期の設定がありません。")
        return s.folder_id, s.folder_path

    def history_load(self) -> "HistoryData":
        """履歴画面に出すもの一式（競合コピー、過去の版、過去の版が使っている容量）。"""
        from folderlink.core import history

        folder_id, folder_path = self._require_folder()
        conflicts = history.list_conflicts(folder_path)
        try:
            versions = history.list_versions(self._client(), folder_id, folder_path)
            versions_error = None
        except history.HistoryError as exc:
            versions, versions_error = [], str(exc)
        used_bytes, used_files = history.versions_disk_usage(folder_path)
        return HistoryData(conflicts, versions, used_bytes, used_files, versions_error)

    def history_restore(self, rel_path: str, version_time: str) -> None:
        from folderlink.core import history

        folder_id, folder_path = self._require_folder()
        history.restore_version(self._client(), folder_id, rel_path, version_time, folder_path)

    def history_resolve_conflict(self, conflict_rel: str, keep: str) -> dict[str, Any]:
        from folderlink.core import history

        folder_id, folder_path = self._require_folder()
        result = history.resolve_conflict(self._client(), folder_id, folder_path, conflict_rel, keep)
        self.invalidate_conflicts()
        return result

    # ---- ログ ----
    def engine_log_tail(self, lines: int = 200) -> str:
        if self.manager is not None:
            return self.manager.log_tail(lines)
        return ""
