"""大量削除の検知（safety_guard）の単体テスト。エンジンは偽物。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fake_client import FakeClient
from folderlink.core.api_client import ApiConnectionError
from folderlink.core.history import VersionEntry
from folderlink.core.safety_guard import (
    HONEST_DESCRIPTION,
    WATCHED_EVENTS,
    DeleteCounter,
    GuardTrip,
    MassDeleteGuard,
    deleted_since,
    restore_deleted,
    trip_message,
)

FID = "fl-test"


def ev(eid, kind, **data):
    data.setdefault("folder", FID)
    return {"id": eid, "type": kind, "data": data}


def summary(eid, need_deletes):
    return {"id": eid, "type": "FolderSummary", "data": {"folder": FID, "summary": {"needDeletes": need_deletes}}}


def make_client():
    client = FakeClient()
    client.folders[FID] = {"id": FID, "paused": False}
    return client


# ---------------------------------------------------------------- 数え方
def test_counter_pending_from_summary_and_applied_deletes():
    t = {"now": 100.0}
    c = DeleteCounter(FID, 5, window_s=60, clock=lambda: t["now"])
    c.feed(summary(1, 3))
    assert c.pending == 3 and not c.tripped()
    for i in range(2):
        c.feed(ev(2 + i, "ItemFinished", action="delete", item=f"f{i}", error=None))
    assert c.applied_count == 2 and c.tripped()
    # 時間がたてば、適用済みの数は忘れる
    t["now"] += 61
    assert c.applied_count == 0


def test_counter_ignores_other_folders_updates_and_failed_deletes():
    c = DeleteCounter(FID, 1)
    c.feed({"id": 1, "type": "FolderSummary", "data": {"folder": "other", "summary": {"needDeletes": 99}}})
    c.feed(ev(2, "ItemFinished", action="update", item="a"))
    c.feed(ev(3, "ItemFinished", action="delete", item="b", error="permission denied"))
    c.feed({"id": 4, "type": "ItemFinished", "data": None})
    c.feed(summary(5, "x"))
    assert not c.tripped()
    assert c.feed(ev(6, "RemoteIndexUpdated", items=10)) is True


# ---------------------------------------------------------------- 検知と一時停止
def test_trip_pauses_folder_immediately_and_once():
    client = make_client()
    trips = []
    guard = MassDeleteGuard(client, FID, 50, on_trip=trips.append)
    assert guard.process([summary(1, 10)]) is None
    assert client.called("patch_folder") == []
    trip = guard.process([summary(2, 300)])
    assert trip is not None and trips == [trip]
    assert client.called("patch_folder") == [(FID, {"paused": True})]
    assert trip.paused and trip.pending_deletes == 300 and trip.applied_deletes == 0
    assert trip.first_delete_at <= trip.detected_at
    # 確認を待つ間は、何度も止めたり通知したりしない
    assert guard.process([summary(3, 400)]) is None and len(trips) == 1
    guard.rearm()
    assert guard.process([summary(4, 60)]) is not None and len(trips) == 2


def test_remote_index_update_triggers_status_query():
    client = make_client()
    client.status[FID] = {"needDeletes": 120}
    guard = MassDeleteGuard(client, FID, 100)
    trip = guard.process([ev(7, "RemoteIndexUpdated", items=120)])
    assert trip is not None and trip.pending_deletes == 120
    assert client.called("db_status")
    assert guard._since == 7


def test_applied_deletes_are_counted_and_named():
    client = make_client()
    guard = MassDeleteGuard(client, FID, 3)
    trip = guard.process([ev(i, "ItemFinished", action="delete", item=f"d/f{i}.txt", error=None) for i in range(1, 4)])
    assert trip is not None and trip.applied_deletes == 3
    assert trip.deleted_names == ["d/f1.txt", "d/f2.txt", "d/f3.txt"]
    assert "止まる前に" in trip_message(trip) and "3 件" in trip_message(trip)


def test_pause_failure_is_reported():
    client = make_client()
    client.fail["patch_folder"] = ApiConnectionError("接続できません")
    trip = MassDeleteGuard(client, FID, 1).process([summary(1, 5)])
    assert trip is not None and not trip.paused and trip.pause_error
    assert "一時停止できませんでした" in trip_message(trip)


def test_start_uses_same_event_filter_for_cursor():
    """実測: イベントの id は絞り込みごとに別の通し番号。始まりの位置も同じ絞り込みで取る。"""
    client = make_client()
    guard = MassDeleteGuard(client, FID, 5, poll_timeout_s=1)
    guard.start()
    try:
        assert guard.running
    finally:
        guard.stop()
    assert not guard.running
    first = client.called("events")[0]
    assert first[2] == WATCHED_EVENTS and first[3] == 1


# ---------------------------------------------------------------- 戻す
def _v(path, minutes_ago, exists=False, vt=None):
    at = datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc) - timedelta(minutes=minutes_ago)
    return VersionEntry(path, vt or at.isoformat(), at, None, 10, exists)


def test_deleted_since_picks_latest_missing_versions_after_trip():
    since = datetime(2026, 11, 21, 3, 50, tzinfo=timezone.utc)
    versions = [
        _v("a.txt", 5), _v("a.txt", 3),                 # 新しいほうを選ぶ
        _v("b.txt", 2, exists=True),                    # いまあるものは戻さない
        _v("c.txt", 30),                                # 検知より前の版は対象外
        _v("d/e.txt", 1),
        VersionEntry("f.txt", "x", None, None, 1, False),  # 時刻が分からないものは対象外
    ]
    chosen = deleted_since(versions, since)
    assert [v.rel_path for v in chosen] == ["a.txt", "d/e.txt"]
    assert chosen[0].archived_at == datetime(2026, 11, 21, 3, 57, tzinfo=timezone.utc)


def test_restore_deleted_batches_and_collects_errors():
    client = make_client()
    entries = [_v(f"f{i}.txt", 1) for i in range(5)]
    client.restore_errors = {"f1.txt": "denied"}
    errors = restore_deleted(client, FID, entries, batch=2)
    assert len(client.called("restore_versions")) == 3
    assert errors == {"f1.txt": "denied"}
    assert client.called("scan")
    client.fail["restore_versions"] = ApiConnectionError("x")
    errors = restore_deleted(client, FID, entries[:2])
    assert set(errors) == {"f0.txt", "f1.txt"}


def test_messages_never_claim_protection():
    """仕様 4章: 確実に止められると確かめていない限り「保護」と書かない。"""
    texts = [HONEST_DESCRIPTION]
    now = datetime.now(timezone.utc)
    for applied, paused in ((0, True), (5, True), (0, False)):
        texts.append(trip_message(GuardTrip(FID, now, now, 100, applied, paused, None if paused else "x")))
    for t in texts:
        assert "保護" not in t
    assert "確実に防ぐ機能ではありません" in HONEST_DESCRIPTION
