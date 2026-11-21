"""大量削除の検知（相手の PC で大量に削除された変更が、このPCに届いたとき）。

しくみ:
- エンジンのイベント（/rest/events）を待ち受け、相手から変更一覧が届いた（RemoteIndexUpdated）・
  フォルダの集計が変わった（FolderSummary）ときに「これから消される件数」（needDeletes）を見る。
- 既に消された件数（ItemFinished の action=delete）も、直近の一定時間ぶん数える。
- 合計がしきい値以上なら、**すぐにフォルダを一時停止**して、残りの削除がこのPCに適用されないようにする。

限界（1台2インスタンスでの実測。tests/integration/test_mass_delete_guard.py）:
- エンジンは届いた削除を自分ですぐに適用し始めるので、**一時停止が間に合わず一部（または全部）が
  先に消えることがある**。消されたファイルは過去の版（.stversions）に残るので、
  `deleted_since()` で集めて戻せる。→ 画面では「保護」ではなく「検知して止め、戻せるようにする」と書く。
- 見ているのは「相手から届いた削除」だけ。このPCで自分が消したものは、相手の PC の側で検知される。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from folderlink.core.api_client import ApiClient, ApiError

log = logging.getLogger(__name__)

WATCHED_EVENTS = ["RemoteIndexUpdated", "FolderSummary", "ItemFinished", "ItemStarted"]


@dataclass
class GuardTrip:
    """検知した1回分。"""

    folder_id: str
    detected_at: datetime            # 検知した時刻（UTC）
    first_delete_at: datetime        # この件の削除が届き始めた時刻（UTC。復元の範囲に使う）
    pending_deletes: int             # 検知した時点で「これから消される」件数
    applied_deletes: int             # 検知した時点で既に消されていた件数
    paused: bool                     # 一時停止できたか
    pause_error: str | None = None
    deleted_names: list[str] = field(default_factory=list)  # 既に消されたファイル（分かった分だけ）

    @property
    def total(self) -> int:
        return self.pending_deletes + self.applied_deletes


class DeleteCounter:
    """イベントを受け取って、しきい値を超えたかを判定する（通信はしない。単体テスト用に分けてある）。"""

    def __init__(self, folder_id: str, threshold: int, window_s: float = 120.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.folder_id = folder_id
        self.threshold = max(1, int(threshold))
        self.window_s = window_s
        self.clock = clock
        self.pending = 0
        self.applied: deque[tuple[float, str]] = deque()
        self.first_seen: float | None = None

    def _trim(self) -> None:
        limit = self.clock() - self.window_s
        while self.applied and self.applied[0][0] < limit:
            self.applied.popleft()
        if not self.applied and self.pending == 0:
            self.first_seen = None

    def set_pending(self, need_deletes: int | None) -> None:
        if isinstance(need_deletes, int) and not isinstance(need_deletes, bool) and need_deletes >= 0:
            self.pending = need_deletes
            if need_deletes and self.first_seen is None:
                self.first_seen = self.clock()

    def feed(self, event: dict[str, Any]) -> bool:
        """イベント1つを取り込む。状態の問い合わせが必要なら True（RemoteIndexUpdated のとき）。"""
        kind = event.get("type")
        data = event.get("data") or {}
        if not isinstance(data, dict) or data.get("folder") != self.folder_id:
            return False
        if kind == "FolderSummary":
            summary = data.get("summary") or {}
            if isinstance(summary, dict):
                self.set_pending(summary.get("needDeletes"))
        elif kind == "ItemFinished":
            if data.get("action") == "delete" and not data.get("error"):
                self.applied.append((self.clock(), str(data.get("item") or "")))
                if self.first_seen is None:
                    self.first_seen = self.clock()
        elif kind == "ItemStarted":
            if data.get("action") == "delete" and self.first_seen is None:
                self.first_seen = self.clock()
        elif kind == "RemoteIndexUpdated":
            return True
        return False

    @property
    def applied_count(self) -> int:
        self._trim()
        return len(self.applied)

    def tripped(self) -> bool:
        return self.pending + self.applied_count >= self.threshold

    def reset(self) -> None:
        self.pending = 0
        self.applied.clear()
        self.first_seen = None


class MassDeleteGuard:
    """別スレッドでイベントを待ち受け、大量削除を検知したらフォルダを一時停止する。

    on_trip は別スレッドから呼ばれる（画面側はシグナル等で受け渡すこと）。
    """

    def __init__(self, client: ApiClient, folder_id: str, threshold: int,
                 on_trip: Callable[[GuardTrip], None] | None = None, window_s: float = 120.0,
                 poll_timeout_s: int = 5) -> None:
        self.client = client
        self.folder_id = folder_id
        self.counter = DeleteCounter(folder_id, threshold, window_s)
        self.on_trip = on_trip
        self.poll_timeout_s = poll_timeout_s
        self.trips: list[GuardTrip] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._since = 0
        self._tripped = False

    # ---- 起動・停止 ----
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._since = self._latest_event_id()
        self._thread = threading.Thread(target=self._run, name="folderlink-mass-delete-guard", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def rearm(self) -> None:
        """利用者が確認を終えたあと、また検知できるようにする。"""
        self.counter.reset()
        self._tripped = False

    def _latest_event_id(self) -> int:
        # 注意（実測）: イベントの id は、絞り込みの種類（events=...）ごとに別の通し番号になる。
        # 始まりの位置は、本番と同じ絞り込みで取らないと、届いたイベントを全部読み飛ばしてしまう。
        try:
            events = self.client.events(since=0, timeout_s=0, events=WATCHED_EVENTS, limit=1)
        except ApiError:
            return 0
        ids = [e.get("id") for e in events if isinstance(e.get("id"), int)]
        return max(ids) if ids else 0

    # ---- 本体 ----
    def _run(self) -> None:
        while not self._stop.is_set():
            began = time.monotonic()
            try:
                events = self.client.events(since=self._since, timeout_s=self.poll_timeout_s,
                                            events=WATCHED_EVENTS)
            except ApiError as exc:
                log.debug("大量削除の検知: イベントを取得できません（%s）", type(exc).__name__)
                self._stop.wait(2.0)
                continue
            except Exception:  # 想定外の失敗でもスレッドを落とさない
                log.exception("大量削除の検知: 想定外のエラー")
                self._stop.wait(5.0)
                continue
            if not events and time.monotonic() - began < 0.2:
                self._stop.wait(0.5)  # 待たずに空で返ってきた（エンジンの再起動中など）。空回りしない
            self.process(events)

    def process(self, events: list[dict[str, Any]]) -> GuardTrip | None:
        """イベントの束を処理する（テストからも直接呼べる）。検知したら GuardTrip を返す。"""
        need_status = False
        for event in events:
            eid = event.get("id")
            if isinstance(eid, int) and eid > self._since:
                self._since = eid
            need_status = self.counter.feed(event) or need_status
            if not self._tripped and self.counter.tripped():
                return self._trip()
        if need_status and not self._tripped:
            try:
                status = self.client.db_status(self.folder_id)
                self.counter.set_pending(status.get("needDeletes"))
            except ApiError:
                pass
            if self.counter.tripped():
                return self._trip()
        return None

    def _trip(self) -> GuardTrip:
        self._tripped = True
        paused, error = True, None
        try:
            self.client.patch_folder(self.folder_id, {"paused": True})
        except ApiError as exc:
            paused, error = False, str(exc)
        now = datetime.now(timezone.utc)
        first = self.counter.first_seen
        lead = (time.monotonic() - first) if first is not None else 0.0
        trip = GuardTrip(
            folder_id=self.folder_id,
            detected_at=now,
            first_delete_at=now - timedelta(seconds=max(0.0, lead) + 2.0),
            pending_deletes=self.counter.pending,
            applied_deletes=self.counter.applied_count,
            paused=paused,
            pause_error=error,
            deleted_names=[name for _t, name in self.counter.applied if name],
        )
        self.trips.append(trip)
        log.warning("大量削除を検知しました（届いた削除 %d 件、うち適用済み %d 件）。一時停止=%s",
                    trip.total, trip.applied_deletes, paused)
        if self.on_trip is not None:
            try:
                self.on_trip(trip)
            except Exception:  # 通知の失敗で検知を止めない
                log.exception("大量削除の通知に失敗しました")
        return trip


#: 画面・README で使う説明。**実測した範囲だけを書く**（「保護」とは書かない）。
HONEST_DESCRIPTION = (
    "相手のPCで一度にたくさんのファイルが削除されたことを検知すると、同期を一時停止して知らせます。"
    "一度に届いた削除は、多くの場合このPCに適用される前に止まります。ただし、少しずつ削除された場合や"
    "回線の状況によっては、止まる前に一部（または全部）が削除されることがあります（確実に防ぐ機能ではありません）。"
    "削除されたファイルは「過去の版」に残っているので、まとめて戻せます。"
    "このPCで自分が削除したファイルは、相手のPCの側で検知されます。"
)


def trip_message(trip: GuardTrip) -> str:
    """検知したときに出す文面。"""
    if not trip.paused:
        return (f"相手のPCで {trip.total} 件のファイルが削除されました。同期を一時停止できませんでした"
                f"（{trip.pause_error or '原因不明'}）。")
    if trip.applied_deletes:
        return (f"相手のPCで {trip.total} 件のファイルが削除されました。同期を一時停止しましたが、"
                f"止まる前にこのPCでも {trip.applied_deletes} 件が削除されました（過去の版から戻せます）。")
    return (f"相手のPCで {trip.total} 件のファイルが削除されました。このPCではまだ削除せず、"
            "同期を一時停止しています。削除を受け入れるか、ファイルを戻すかを選んでください。")


def deleted_since(versions: list, since: datetime) -> list:
    """過去の版の一覧から、since 以降に退避され、いまは存在しないファイルの最新の版を選ぶ。"""
    chosen: dict[str, Any] = {}
    for v in versions:
        if v.current_exists or v.archived_at is None:
            continue
        at = v.archived_at if v.archived_at.tzinfo is not None else v.archived_at.astimezone()
        if at < since:
            continue
        best = chosen.get(v.rel_path)
        if best is None or v.version_time > best.version_time:
            chosen[v.rel_path] = v
    return sorted(chosen.values(), key=lambda v: v.rel_path)


def restore_deleted(client: ApiClient, folder_id: str, entries: list, batch: int = 500) -> dict[str, str]:
    """消されたファイルをまとめて戻す。失敗したものの {パス: 理由} を返す。"""
    errors: dict[str, str] = {}
    for start in range(0, len(entries), batch):
        chunk = {v.rel_path: v.version_time for v in entries[start:start + batch]}
        try:
            errors.update(client.restore_versions(folder_id, chunk) or {})
        except ApiError as exc:
            errors.update({k: str(exc) for k in chunk})
    try:
        client.scan(folder_id)
    except ApiError:
        pass
    return errors
