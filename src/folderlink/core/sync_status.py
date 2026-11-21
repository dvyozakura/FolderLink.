"""同期状態の判定と数値の取得。

方針（仕様 3章・6章-2）:
- 状態は「同期完了／同期中／確認中／オフライン／一時停止／競合あり／エラー／未設定／起動中」。
- `/rest/db/completion` の `completion` が 100 でも同期が終わっていない瞬間があるので、
  `/rest/db/status` の `state`・`need*`、相手との接続状態、相手側の残りも合わせて判定する。
- 取れなかった値は None のままにする（画面では「—」）。それらしい数字を作らない。
- 一時停止中は状態の取得がエラーになることがあるので、設定の `paused` を先に見て、
  エラーになっても「一時停止」として扱う。
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from folderlink.core.api_client import (
    ApiClient,
    ApiConnectionError,
    ApiError,
    ApiFolderPausedError,
    ApiNotFoundError,
)

log = logging.getLogger(__name__)


class SyncState(enum.Enum):
    SYNCED = "synced"
    SYNCING = "syncing"
    SCANNING = "scanning"
    OFFLINE = "offline"
    PAUSED = "paused"
    CONFLICT = "conflict"
    ERROR = "error"
    NOT_CONFIGURED = "not_configured"
    STARTING = "starting"

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def color(self) -> str:
        """画面の色分け: green / blue / yellow / red / gray。"""
        return _COLORS[self]


_LABELS = {
    SyncState.SYNCED: "同期完了",
    SyncState.SYNCING: "同期中",
    SyncState.SCANNING: "確認中",
    SyncState.OFFLINE: "オフライン",
    SyncState.PAUSED: "一時停止",
    SyncState.CONFLICT: "競合あり",
    SyncState.ERROR: "エラー",
    SyncState.NOT_CONFIGURED: "未設定",
    SyncState.STARTING: "起動中",
}

_COLORS = {
    SyncState.SYNCED: "green",
    SyncState.SYNCING: "blue",
    SyncState.SCANNING: "blue",
    SyncState.OFFLINE: "gray",
    SyncState.PAUSED: "yellow",
    SyncState.CONFLICT: "yellow",
    SyncState.ERROR: "red",
    SyncState.NOT_CONFIGURED: "gray",
    SyncState.STARTING: "gray",
}

_SYNCING_STATES = frozenset({"syncing", "sync-preparing", "sync-waiting", "cleaning", "clean-waiting"})
_SCANNING_STATES = frozenset({"scanning", "scan-waiting"})


@dataclass
class StatusInputs:
    """判定の材料。取れなかったものは None。"""

    engine_reachable: bool = True
    engine_error: str | None = None
    folder_configured: bool | None = None
    peer_configured: bool | None = None
    folder_paused: bool | None = None
    peer_paused: bool | None = None
    folder_state: str | None = None          # idle / scanning / syncing / error ...
    folder_error: str | None = None          # フォルダ全体のエラー（マーカーが無い、容量不足など）
    pull_errors: int | None = None           # 受け取れなかった項目の数
    pull_error_detail: str | None = None     # その1件目の説明（日本語）
    need_items: int | None = None            # 自分側がまだ受け取っていない項目
    need_bytes: int | None = None
    peer_connected: bool | None = None
    remote_completion: float | None = None   # 相手側の完了率（0〜100）
    remote_need_items: int | None = None     # 相手側がまだ受け取っていない項目（削除を含む）
    remote_state: str | None = None          # valid / paused / notSharing / unknown
    conflicts: int | None = None             # 競合コピーの数


def decide_state(i: StatusInputs) -> tuple[SyncState, str]:
    """材料から状態と一言の説明を決める（副作用なし）。"""
    if not i.engine_reachable:
        return SyncState.ERROR, i.engine_error or "同期エンジンに接続できません。"
    if i.folder_configured is False or i.peer_configured is False:
        return SyncState.NOT_CONFIGURED, "初期設定がまだ終わっていません。"
    if i.folder_error:
        return SyncState.ERROR, i.folder_error
    if i.folder_state == "error":
        return SyncState.ERROR, "フォルダでエラーが起きています。ログを確認してください。"
    if i.folder_paused or i.peer_paused:
        return SyncState.PAUSED, "同期を一時停止しています。"
    if i.pull_errors:
        detail = f"（{i.pull_error_detail}）" if i.pull_error_detail else ""
        return SyncState.ERROR, f"同期できなかった項目が {i.pull_errors} 件あります。{detail}"
    if i.folder_state is None and i.need_items is None:
        return SyncState.ERROR, "フォルダの状態を取得できません。"

    local_pending = bool(i.need_items) or bool(i.need_bytes)
    remote_pending = bool(i.remote_need_items) or (i.remote_completion is not None and i.remote_completion < 100)

    if i.peer_connected:
        if i.remote_state == "notSharing":
            return SyncState.OFFLINE, "相手のPCで、このフォルダの共有がまだ承認されていません。"
        if i.remote_state == "paused":
            return SyncState.PAUSED, "相手のPCで同期が一時停止されています。"
        if i.folder_state in _SYNCING_STATES or local_pending or remote_pending:
            return SyncState.SYNCING, "同期しています。"
    if i.folder_state in _SCANNING_STATES:
        return SyncState.SCANNING, "フォルダの変更を確認しています。"
    if i.conflicts:
        return SyncState.CONFLICT, f"競合したファイルが {i.conflicts} 件あります。履歴から確認してください。"
    if not i.peer_connected:
        if local_pending:
            return SyncState.OFFLINE, "相手のPCに接続できていません。接続できしだい続きを同期します。"
        return SyncState.OFFLINE, "相手のPCに接続できていません。"
    # ここまで来たら: 接続中・idle・自分側も相手側も残りなし
    if i.folder_state != "idle" or i.need_items is None:
        return SyncState.SYNCING, "同期の状態を確認しています。"
    if i.remote_completion is None or i.remote_state not in ("valid", None):
        return SyncState.SYNCING, "相手のPCの状態を確認しています。"
    return SyncState.SYNCED, "すべて同期されています。"


@dataclass
class StatusSnapshot:
    """画面に出す1回分の状態。None は「取得できなかった」。"""

    state: SyncState
    message: str
    checked_at: datetime
    completion: float | None = None      # 全体の完了率（自分側と相手側の小さいほう）
    need_items: int | None = None        # 未同期の件数（自分側と相手側の合計）
    need_bytes: int | None = None
    global_files: int | None = None
    global_bytes: int | None = None
    local_files: int | None = None
    local_bytes: int | None = None
    in_bps: float | None = None          # 受信速度（バイト/秒）
    out_bps: float | None = None
    last_sync: datetime | None = None    # 最後にファイルを同期した時刻
    last_file: str | None = None
    peer_connected: bool | None = None
    peer_paused: bool | None = None
    peer_address: str | None = None
    peer_via_relay: bool | None = None
    conflicts: int | None = None
    errors: list[str] = field(default_factory=list)
    guard_trip: Any = None               # safety_guard.GuardTrip（大量削除を検知して確認待ち）


class RateMeter:
    """転送量の累計から速度を出す。1回目や、累計が巻き戻ったときは None（捏造しない）。"""

    def __init__(self) -> None:
        self._last: tuple[float, int, int] | None = None

    def update(self, at: float | None, in_total: int | None, out_total: int | None
               ) -> tuple[float | None, float | None]:
        if at is None or in_total is None or out_total is None:
            self._last = None
            return None, None
        last, self._last = self._last, (at, in_total, out_total)
        if last is None:
            return None, None
        dt = at - last[0]
        d_in, d_out = in_total - last[1], out_total - last[2]
        if dt <= 0.2 or d_in < 0 or d_out < 0:
            return None, None
        return d_in / dt, d_out / dt

    def reset(self) -> None:
        self._last = None


def parse_time(value: Any) -> datetime | None:
    """Syncthing の時刻（RFC 3339、ナノ秒つき）を datetime に。未設定（0001年）は None。"""
    if not isinstance(value, str) or not value or value.startswith("0001-") or value.startswith("1970-01-01T00:00:00"):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # 小数部を 6 桁（マイクロ秒）に切り詰める
    if "." in text:
        head, rest = text.split(".", 1)
        digits = ""
        for ch in rest:
            if ch.isdigit():
                digits += ch
            else:
                break
        tail = rest[len(digits):]
        text = f"{head}.{digits[:6].ljust(6, '0')}{tail}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.year < 2000:
        return None
    return parsed


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class StatusCollector:
    """エンジンから材料を集めて StatusSnapshot を作る。途中の失敗では落ちない。"""

    def __init__(
        self,
        client: ApiClient,
        folder_id: str | None,
        peer_id: str | None,
        *,
        conflict_counter: Callable[[], int | None] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.client = client
        self.folder_id = folder_id
        self.peer_id = peer_id
        self._conflict_counter = conflict_counter
        self._now = now
        self._rate = RateMeter()

    def collect(self) -> StatusSnapshot:
        now = self._now()
        inp = StatusInputs()
        snap = StatusSnapshot(state=SyncState.ERROR, message="", checked_at=now)

        if not self.folder_id or not self.peer_id:
            inp.folder_configured = bool(self.folder_id)
            inp.peer_configured = bool(self.peer_id)
            snap.state, snap.message = decide_state(inp)
            return snap

        # --- 設定（一時停止かどうかは、状態の取得より先に設定で見る） ---
        try:
            folder_cfg = self.client.get_folder(self.folder_id)
            inp.folder_configured = True
            inp.folder_paused = bool(folder_cfg.get("paused"))
        except ApiNotFoundError:
            inp.folder_configured = False
        except ApiConnectionError as exc:
            return self._unreachable(snap, inp, exc)
        except ApiError as exc:
            snap.errors.append(str(exc))
        try:
            device_cfg = self.client.get_device(self.peer_id)
            inp.peer_configured = True
            inp.peer_paused = bool(device_cfg.get("paused"))
        except ApiNotFoundError:
            inp.peer_configured = False
        except ApiConnectionError as exc:
            return self._unreachable(snap, inp, exc)
        except ApiError as exc:
            snap.errors.append(str(exc))

        if inp.folder_configured is False or inp.peer_configured is False:
            snap.state, snap.message = decide_state(inp)
            return snap

        # --- 接続 ---
        try:
            conns = self.client.connections()
            info = (conns.get("connections") or {}).get(self.peer_id)
            if isinstance(info, dict):
                inp.peer_connected = bool(info.get("connected"))
                if info.get("paused"):
                    inp.peer_paused = True
                if inp.peer_connected:
                    snap.peer_address = str(info.get("address") or "") or None
                    conn_type = str(info.get("type") or "")
                    snap.peer_via_relay = ("relay" in conn_type) if conn_type else None
            else:
                inp.peer_connected = False
            total = conns.get("total") or {}
            at = parse_time(total.get("at"))
            snap.in_bps, snap.out_bps = self._rate.update(
                at.timestamp() if at else None, _int(total.get("inBytesTotal")), _int(total.get("outBytesTotal")))
            if not inp.peer_connected:
                # 繋がっていないときの速度は 0 と分かっている
                snap.in_bps, snap.out_bps = (0.0, 0.0) if snap.in_bps is not None else (None, None)
        except ApiConnectionError as exc:
            return self._unreachable(snap, inp, exc)
        except ApiError as exc:
            snap.errors.append(str(exc))
            self._rate.reset()
        snap.peer_connected = inp.peer_connected
        snap.peer_paused = inp.peer_paused

        # --- 自分側のフォルダの状態 ---
        local_done: float | None = None
        if not inp.folder_paused:
            try:
                st = self.client.db_status(self.folder_id)
                inp.folder_state = str(st.get("state") or "") or None
                inp.need_bytes = _int(st.get("needBytes"))
                inp.need_items = _int(st.get("needTotalItems"))
                if inp.need_items is None:
                    parts = [_int(st.get(k)) for k in ("needFiles", "needDirectories", "needSymlinks", "needDeletes")]
                    inp.need_items = sum(p for p in parts if p is not None) if any(p is not None for p in parts) else None
                inp.pull_errors = _int(st.get("pullErrors"))
                if inp.pull_errors:
                    try:
                        items = self.client.folder_errors(self.folder_id)
                        first = items[0] if items and isinstance(items[0], dict) else None
                        if first:
                            inp.pull_error_detail = file_error_text(str(first.get("path") or ""),
                                                                    str(first.get("error") or ""))
                    except ApiConnectionError:
                        raise
                    except ApiError:
                        pass
                err = st.get("error")
                if isinstance(err, str) and err.strip():
                    inp.folder_error = _folder_error_text(err)
                snap.global_bytes = _int(st.get("globalBytes"))
                snap.global_files = _int(st.get("globalFiles"))
                snap.local_bytes = _int(st.get("localBytes"))
                snap.local_files = _int(st.get("localFiles"))
                if snap.global_bytes is not None and inp.need_bytes is not None:
                    if snap.global_bytes > 0:
                        local_done = max(0.0, min(100.0, 100.0 * (snap.global_bytes - inp.need_bytes) / snap.global_bytes))
                    else:
                        # 容量 0 で残りがある（フォルダ作成・削除だけ）場合は、率を出せない
                        local_done = 100.0 if not inp.need_items else None
            except ApiFolderPausedError:
                inp.folder_paused = True
            except ApiConnectionError as exc:
                return self._unreachable(snap, inp, exc)
            except ApiError as exc:
                snap.errors.append(str(exc))

        # --- 相手側の進み具合 ---
        if not inp.folder_paused and inp.peer_connected:
            try:
                comp = self.client.db_completion(self.folder_id, self.peer_id)
                inp.remote_completion = _float(comp.get("completion"))
                items, deletes = _int(comp.get("needItems")), _int(comp.get("needDeletes"))
                if items is not None or deletes is not None:
                    inp.remote_need_items = (items or 0) + (deletes or 0)
                inp.remote_state = str(comp.get("remoteState") or "") or None
            except ApiFolderPausedError:
                inp.folder_paused = True
            except ApiConnectionError as exc:
                return self._unreachable(snap, inp, exc)
            except ApiError as exc:
                snap.errors.append(str(exc))

        # --- 最終同期時刻 ---
        try:
            stats = self.client.stats_folder().get(self.folder_id) or {}
            last_file = stats.get("lastFile") or {}
            snap.last_sync = parse_time(last_file.get("at"))
            snap.last_file = (str(last_file.get("filename")) or None) if snap.last_sync else None
        except ApiError:
            pass

        # --- 競合コピー ---
        if self._conflict_counter is not None:
            try:
                inp.conflicts = self._conflict_counter()
            except Exception as exc:  # 数えられなくても状態表示は続ける
                log.debug("競合コピーを数えられませんでした: %s", type(exc).__name__)
                inp.conflicts = None
        snap.conflicts = inp.conflicts

        # --- まとめ ---
        if inp.folder_paused:
            snap.completion = None
            snap.need_items = None
            snap.need_bytes = None
            snap.in_bps = snap.out_bps = None
            # 一時停止中の /rest/db/status は「全部 0」を返す（実物で確認）。それを件数として出さない。
            snap.global_files = snap.global_bytes = snap.local_files = snap.local_bytes = None
        else:
            if local_done is not None and inp.remote_completion is not None:
                snap.completion = min(local_done, inp.remote_completion)
            elif local_done is not None and inp.peer_connected is False:
                snap.completion = None  # 相手側が分からないので、全体の完了率は出さない
            if inp.need_items is not None:
                snap.need_items = inp.need_items + (inp.remote_need_items or 0 if inp.peer_connected else 0)
                if inp.peer_connected and inp.remote_need_items is None:
                    snap.need_items = None  # 相手側の残りが分からない
            snap.need_bytes = inp.need_bytes
        snap.state, snap.message = decide_state(inp)
        if snap.state is SyncState.SYNCED:
            snap.completion = 100.0
        elif snap.completion is not None and snap.completion >= 100.0:
            snap.completion = 99.0  # 終わっていないのに 100% と出さない
        return snap

    def _unreachable(self, snap: StatusSnapshot, inp: StatusInputs, exc: ApiError) -> StatusSnapshot:
        inp.engine_reachable = False
        self._rate.reset()
        snap.state, snap.message = decide_state(inp)
        snap.errors.append(str(exc))
        return snap


def _folder_error_text(raw: str) -> str:
    """フォルダのエラー文（英語）を、分かる範囲で日本語にする。"""
    low = raw.lower()
    if "folder marker missing" in low:
        return "同期フォルダの目印（.stfolder）が見つかりません。フォルダが移動・削除されたか、ドライブが外れている可能性があります。"
    if "folder path missing" in low:
        return "同期フォルダが見つかりません。フォルダが移動・削除されたか、ドライブが外れている可能性があります。"
    if "insufficient space" in low or "disk space" in low or "not enough" in low:
        return "ディスクの空き容量が足りません。"
    if "permission denied" in low or "access is denied" in low:
        return "フォルダにアクセスする権限がありません。"
    return f"フォルダでエラーが起きています: {raw}"


def file_error_text(path: str, raw: str) -> str:
    """1つのファイルの同期エラー（/rest/folder/errors。英語）を、分かる範囲で日本語にする（実物の文面で確認）。"""
    low = (raw or "").lower()
    name = path or "（不明）"
    if "different upper or lowercase" in low or "case conflict" in low:
        return (f"「{name}」: 大文字と小文字だけが違う同じ名前のファイルが、2台にそれぞれあります。"
                "どちらかの名前を変えてください（Windows では区別できません）。")
    if "being used by another process" in low or "sharing violation" in low:
        return f"「{name}」: ファイルが別のアプリで開かれているため更新できません。閉じると自動で続きを同期します。"
    if "insufficient space" in low or "not enough space" in low or "disk full" in low:
        return f"「{name}」: ディスクの空き容量が足りません。"
    if "permission denied" in low or "access is denied" in low:
        return f"「{name}」: アクセスする権限がありません。"
    return f"「{name}」: {raw}"
