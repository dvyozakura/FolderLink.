"""結合テスト（1台2インスタンス）: 大量削除の検知が、相手の削除が適用される**前に**止められるかを測る。

結果は TEST_REPORT.md 項目12 に記録。これは「1台の中での確認」で、回線越しでは届く速さが違う。
"""

from __future__ import annotations

import os
import time

from it_helpers import wait_until
from folderlink.core import history
from folderlink.core.safety_guard import MassDeleteGuard, deleted_since, restore_deleted


def write(path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def _populate(p, count: int, size: int = 2000) -> None:
    for i in range(count):
        write(p.a.folder / f"d{i % 10}" / f"file_{i:04d}.txt", f"{i}:" + "x" * size)
    p.settle(180)


def _count_files(folder) -> int:
    return sum(1 for f in folder.rglob("file_*.txt") if ".stversions" not in f.parts)


def _run_case(p, count: int, threshold: int, delete_how: str):
    trips = []
    guard = MassDeleteGuard(p.b.client, p.folder_id, threshold, on_trip=trips.append, poll_timeout_s=2)
    guard.start()
    try:
        time.sleep(0.5)
        files = sorted(p.a.folder.rglob("file_*.txt"))
        if delete_how == "all_at_once":
            for f in files:
                os.remove(f)
            p.a.client.scan(p.folder_id)
        else:  # 少しずつ（監視で拾われる。保存先を整理するような消し方）
            for i, f in enumerate(files):
                os.remove(f)
                if i % 20 == 19:
                    time.sleep(0.5)
        wait_until(lambda: trips, 90, desc="大量削除の検知")
        time.sleep(3)  # 一時停止のあとで、さらに消されないこと
        remaining = _count_files(p.b.folder)
        paused = bool(p.b.client.get_folder(p.folder_id).get("paused"))
    finally:
        guard.stop()
    return trips[0], remaining, paused


def test_small_delete_does_not_trip(new_pair):
    p = new_pair().start()
    p.wait_connected()
    _populate(p, 20)
    trips = []
    guard = MassDeleteGuard(p.b.client, p.folder_id, 50, on_trip=trips.append, poll_timeout_s=1)
    guard.start()
    try:
        for f in sorted(p.a.folder.rglob("file_*.txt"))[:10]:
            os.remove(f)
        p.settle()
        time.sleep(2)
    finally:
        guard.stop()
    assert trips == []
    assert _count_files(p.b.folder) == 10
    assert not p.b.client.get_folder(p.folder_id).get("paused")


def test_mass_delete_all_at_once_measure_and_recover(new_pair):
    """A で 300 件を一度に削除 → B の検知。間に合った件数を測り、消された分を過去の版から戻す。"""
    p = new_pair().start()
    p.wait_connected()
    count = 300
    _populate(p, count)
    started = time.monotonic()
    trip, remaining, paused = _run_case(p, count, threshold=50, delete_how="all_at_once")
    print(f"\n[一度に削除] 件数={count} しきい値=50 検知までの秒={time.monotonic() - started:.2f} "
          f"検知時: 未適用={trip.pending_deletes} 適用済み={trip.applied_deletes} 一時停止={trip.paused} "
          f"3秒後に B に残っていた={remaining}/{count} 一時停止中={paused}")
    assert trip.paused and paused
    assert trip.total >= 50
    # 一時停止のあとは、もう消されない
    time.sleep(2)
    assert _count_files(p.b.folder) == remaining

    # 戻す: 再開 → 残りの削除も適用される → 過去の版から全部戻す → 両側に戻る
    p.b.client.patch_folder(p.folder_id, {"paused": False})
    wait_until(lambda: _count_files(p.b.folder) == 0, 120, desc="残りの削除の適用")
    p.settle(180)
    versions = history.list_versions(p.b.client, p.folder_id, p.b.folder)
    entries = deleted_since(versions, trip.first_delete_at)
    assert len(entries) == count, len(entries)
    errors = restore_deleted(p.b.client, p.folder_id, entries)
    assert errors == {}
    wait_until(lambda: _count_files(p.a.folder) == count, 180, desc="A にも戻る")
    p.settle(180)
    assert _count_files(p.b.folder) == count


def test_mass_delete_gradual_measure(new_pair):
    """A で 300 件を少しずつ削除（監視で拾われる）→ B の検知と、間に合った件数。"""
    p = new_pair().start()
    p.wait_connected()
    count = 300
    _populate(p, count)
    started = time.monotonic()
    trip, remaining, paused = _run_case(p, count, threshold=50, delete_how="gradual")
    print(f"\n[少しずつ削除] 件数={count} しきい値=50 検知までの秒={time.monotonic() - started:.2f} "
          f"検知時: 未適用={trip.pending_deletes} 適用済み={trip.applied_deletes} 一時停止={trip.paused} "
          f"3秒後に B に残っていた={remaining}/{count} 一時停止中={paused}")
    assert trip.paused and paused
