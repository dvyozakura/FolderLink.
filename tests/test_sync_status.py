"""sync_status: 状態の判定、完了率・速度・未同期件数・最終同期時刻。取れない値は None。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fake_client import MY_ID, FakeClient
from folderlink.core import formatting as fm
from folderlink.core.api_client import ApiConnectionError, ApiFolderPausedError, ApiServerError
from folderlink.core.sync_status import (
    RateMeter,
    StatusCollector,
    StatusInputs,
    SyncState,
    decide_state,
    parse_time,
)

FID = "fl-abcde-fghij"
PEER = "P56IOI7-MZJNU2Y-IQGDREY-DM2MGTI-MGL3BXN-PQ6W5BM-TBBZ4TJ-XZWICQ2"
T0 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)


def healthy(**over) -> StatusInputs:
    base = dict(folder_configured=True, peer_configured=True, folder_paused=False, peer_paused=False,
                folder_state="idle", need_items=0, need_bytes=0, pull_errors=0, peer_connected=True,
                remote_completion=100.0, remote_need_items=0, remote_state="valid", conflicts=0)
    base.update(over)
    return StatusInputs(**base)


# ---------------------------------------------------------------- 判定
def test_there_are_at_least_six_states_with_japanese_labels():
    labels = {s.label for s in SyncState}
    assert {"同期完了", "同期中", "オフライン", "一時停止", "競合あり", "エラー"} <= labels
    assert len(labels) == len(SyncState) >= 6
    assert SyncState.SYNCED.color == "green" and SyncState.ERROR.color == "red"
    assert SyncState.PAUSED.color == "yellow" and SyncState.CONFLICT.color == "yellow"


@pytest.mark.parametrize("inputs,expected", [
    (healthy(), SyncState.SYNCED),
    (healthy(folder_state="syncing", need_items=3, need_bytes=10), SyncState.SYNCING),
    (healthy(folder_state="sync-preparing"), SyncState.SYNCING),
    (healthy(folder_state="scanning"), SyncState.SCANNING),
    (healthy(peer_connected=False, remote_completion=None, remote_need_items=None, remote_state=None),
     SyncState.OFFLINE),
    (healthy(folder_paused=True, folder_state=None, need_items=None), SyncState.PAUSED),
    (healthy(peer_paused=True), SyncState.PAUSED),
    (healthy(conflicts=2), SyncState.CONFLICT),
    (healthy(folder_state="error"), SyncState.ERROR),
    (healthy(pull_errors=4), SyncState.ERROR),
    (healthy(folder_error="ディスクの空き容量が足りません。"), SyncState.ERROR),
    (StatusInputs(engine_reachable=False), SyncState.ERROR),
    (StatusInputs(folder_configured=False, peer_configured=True), SyncState.NOT_CONFIGURED),
    (StatusInputs(folder_configured=True, peer_configured=False), SyncState.NOT_CONFIGURED),
])
def test_decide_state(inputs, expected):
    state, message = decide_state(inputs)
    assert state is expected
    assert message


def test_completion_100_alone_is_not_synced():
    """completion が 100 でも、残りがある・状態が idle でない・相手が未確認なら「同期完了」にしない。"""
    assert decide_state(healthy(need_items=1))[0] is SyncState.SYNCING
    assert decide_state(healthy(need_bytes=5))[0] is SyncState.SYNCING
    assert decide_state(healthy(remote_need_items=2))[0] is SyncState.SYNCING  # 相手側に削除などが残っている
    assert decide_state(healthy(remote_completion=99.9))[0] is SyncState.SYNCING
    assert decide_state(healthy(remote_completion=None))[0] is not SyncState.SYNCED
    assert decide_state(healthy(folder_state="sync-waiting"))[0] is SyncState.SYNCING
    assert decide_state(healthy(need_items=None, folder_state="idle"))[0] is not SyncState.SYNCED


def test_remote_not_sharing_and_remote_paused():
    state, msg = decide_state(healthy(remote_state="notSharing", remote_completion=0))
    assert state is SyncState.OFFLINE and "承認" in msg
    assert decide_state(healthy(remote_state="paused"))[0] is SyncState.PAUSED


def test_priorities():
    # エラーは一時停止より先、一時停止は同期中より先
    assert decide_state(healthy(folder_error="x", folder_paused=True))[0] is SyncState.ERROR
    assert decide_state(healthy(folder_paused=True, need_items=5))[0] is SyncState.PAUSED
    # 同期中は競合より先（終わってから競合を知らせる）
    assert decide_state(healthy(conflicts=1, need_items=5, folder_state="syncing"))[0] is SyncState.SYNCING
    # 競合はオフラインより先
    assert decide_state(healthy(conflicts=1, peer_connected=False))[0] is SyncState.CONFLICT
    # オフラインで残りがある
    state, msg = decide_state(healthy(peer_connected=False, need_items=3))
    assert state is SyncState.OFFLINE and "続き" in msg


def test_unknown_folder_state_is_error_not_synced():
    assert decide_state(StatusInputs(folder_configured=True, peer_configured=True, folder_paused=False))[0] \
        is SyncState.ERROR


# ---------------------------------------------------------------- 速度
def test_rate_meter():
    m = RateMeter()
    assert m.update(100.0, 0, 0) == (None, None)       # 1回目は分からない
    assert m.update(102.0, 2048, 1024) == (1024.0, 512.0)
    assert m.update(104.0, 2048, 1024) == (0.0, 0.0)
    assert m.update(106.0, 10, 10) == (None, None)     # 累計が巻き戻った（再接続）→ 出さない
    assert m.update(106.0, 20, 20) == (None, None)     # 時間が進んでいない
    assert m.update(None, 1, 1) == (None, None)
    assert m.update(110.0, 100, 100) == (None, None)   # 取れなかった次も1回目扱い


# ---------------------------------------------------------------- 時刻
def test_parse_time():
    assert parse_time("2026-11-21T12:34:56.123456789+09:00") == \
        datetime(2026, 11, 21, 12, 34, 56, 123456, tzinfo=timezone(timedelta(hours=9)))
    assert parse_time("2026-11-21T03:34:56Z") == datetime(2026, 11, 21, 3, 34, 56, tzinfo=timezone.utc)
    assert parse_time("2026-11-21T03:34:56.5Z").microsecond == 500000
    for empty in ("0001-01-01T00:00:00Z", "1970-01-01T00:00:00Z", "", None, 123, "not a time"):
        assert parse_time(empty) is None


# ---------------------------------------------------------------- 収集
def make_client() -> FakeClient:
    c = FakeClient()
    c.devices[PEER] = {"deviceID": PEER, "name": "PC-B", "paused": False}
    c.folders[FID] = {"id": FID, "path": "C:/x", "paused": False}
    c.conns = {
        "connections": {PEER: {"connected": True, "paused": False, "address": "203.0.113.5:22000",
                               "type": "tcp-client"}},
        "total": {"at": "2026-11-21T12:00:00Z", "inBytesTotal": 1000, "outBytesTotal": 2000},
    }
    c.status[FID] = {"state": "idle", "needBytes": 0, "needTotalItems": 0, "pullErrors": 0,
                     "globalBytes": 1_800_000_000, "globalFiles": 248, "localBytes": 1_800_000_000,
                     "localFiles": 248}
    c.completion[(FID, PEER)] = {"completion": 100, "needItems": 0, "needDeletes": 0, "remoteState": "valid"}
    c.folder_stats = {FID: {"lastFile": {"filename": "a/b.txt", "at": "2026-11-21T11:59:00Z"}}}
    return c


def collector(c, **kw) -> StatusCollector:
    return StatusCollector(c, FID, PEER, now=lambda: T0, **kw)


def test_collect_synced():
    snap = collector(make_client(), conflict_counter=lambda: 0).collect()
    assert snap.state is SyncState.SYNCED
    assert snap.completion == 100.0 and snap.need_items == 0 and snap.need_bytes == 0
    assert snap.global_files == 248 and snap.global_bytes == 1_800_000_000
    assert snap.last_sync == datetime(2026, 11, 21, 11, 59, tzinfo=timezone.utc) and snap.last_file == "a/b.txt"
    assert snap.peer_connected is True and snap.peer_via_relay is False
    assert snap.peer_address == "203.0.113.5:22000"
    assert snap.checked_at == T0
    # 速度は1回目は分からない → None（0 と決めつけない）
    assert snap.in_bps is None and snap.out_bps is None


def test_collect_rates_on_second_sample():
    c = make_client()
    col = collector(c)
    col.collect()
    c.conns["total"] = {"at": "2026-11-21T12:00:02Z", "inBytesTotal": 1000 + 4096, "outBytesTotal": 2000}
    snap = col.collect()
    assert snap.in_bps == 2048.0 and snap.out_bps == 0.0


def test_collect_syncing_reports_min_completion_and_total_need():
    c = make_client()
    c.status[FID].update({"state": "syncing", "needBytes": 900_000_000, "needTotalItems": 10})
    c.completion[(FID, PEER)] = {"completion": 80, "needItems": 3, "needDeletes": 2, "remoteState": "valid"}
    snap = collector(c).collect()
    assert snap.state is SyncState.SYNCING
    assert snap.completion == 50.0  # 自分側 50%、相手側 80% → 小さいほう
    assert snap.need_items == 15    # 自分側 10 + 相手側 3+2
    assert snap.need_bytes == 900_000_000


def test_collect_never_shows_100_percent_while_not_synced():
    c = make_client()
    c.status[FID].update({"state": "syncing"})  # 数字は全部そろっているが、まだ動いている
    snap = collector(c).collect()
    assert snap.state is SyncState.SYNCING
    assert snap.completion is not None and snap.completion < 100
    assert fm.fmt_percent(snap.completion) != "100%"


def test_collect_remote_has_only_deletes_left():
    """相手側の completion が 100 でも、削除が残っていれば同期完了にしない。"""
    c = make_client()
    c.completion[(FID, PEER)] = {"completion": 100, "needItems": 0, "needDeletes": 7, "remoteState": "valid"}
    snap = collector(c).collect()
    assert snap.state is SyncState.SYNCING and snap.need_items == 7


def test_collect_paused_by_config_does_not_query_status():
    c = make_client()
    c.folders[FID]["paused"] = True
    c.fail["db_status"] = AssertionError("一時停止中に状態を問い合わせてはいけない")
    c.fail["db_completion"] = AssertionError("一時停止中に状態を問い合わせてはいけない")
    snap = collector(c).collect()
    assert snap.state is SyncState.PAUSED
    assert snap.completion is None and snap.need_items is None and snap.in_bps is None
    assert fm.fmt_percent(snap.completion) == "—"


def test_collect_paused_error_from_engine_is_treated_as_paused():
    """設定では動いているのに、状態の取得が「一時停止中」のエラーを返した場合。"""
    c = make_client()
    c.fail["db_status"] = ApiFolderPausedError("フォルダは一時停止中です。", status=500)
    assert collector(c).collect().state is SyncState.PAUSED
    c = make_client()
    c.fail["db_completion"] = ApiFolderPausedError("フォルダは一時停止中です。", status=404)
    assert collector(c).collect().state is SyncState.PAUSED


def test_collect_paused_zeros_from_engine_are_not_shown():
    """実物の挙動: 一時停止中の db_status は 200 で「全部 0・state 空」、db_completion は 404。"""
    c = make_client()
    c.status[FID] = {"state": "", "needBytes": 0, "needTotalItems": 0, "pullErrors": 0, "globalBytes": 0,
                     "globalFiles": 0, "localBytes": 0, "localFiles": 0, "error": ""}
    c.fail["db_completion"] = ApiFolderPausedError("フォルダは一時停止中です。", status=404)
    snap = collector(c).collect()
    assert snap.state is SyncState.PAUSED
    assert snap.global_files is None and snap.global_bytes is None
    assert snap.local_files is None and snap.completion is None and snap.need_items is None


def test_collect_device_paused():
    c = make_client()
    c.devices[PEER]["paused"] = True
    c.conns["connections"][PEER] = {"connected": False, "paused": True}
    assert collector(c).collect().state is SyncState.PAUSED


def test_collect_offline_keeps_unknowns_as_none():
    c = make_client()
    c.conns["connections"][PEER] = {"connected": False, "paused": False, "address": "", "type": ""}
    c.fail["db_completion"] = AssertionError("未接続の相手の進み具合は問い合わせない")
    snap = collector(c).collect()
    assert snap.state is SyncState.OFFLINE
    assert snap.peer_connected is False and snap.peer_address is None and snap.peer_via_relay is None
    assert snap.completion is None      # 相手側が分からないので、全体の率は出さない
    assert snap.need_items == 0         # 自分側の残りは分かっている


def test_collect_relay_connection():
    c = make_client()
    c.conns["connections"][PEER]["type"] = "relay-client"
    assert collector(c).collect().peer_via_relay is True


def test_collect_engine_unreachable():
    c = make_client()
    c.fail["get_folder"] = ApiConnectionError("同期エンジンに接続できません。")
    snap = collector(c).collect()
    assert snap.state is SyncState.ERROR and "接続" in snap.message
    assert snap.completion is None and snap.need_items is None and snap.last_sync is None
    assert snap.global_files is None and snap.in_bps is None


def test_collect_survives_partial_failures():
    c = make_client()
    c.fail["stats_folder"] = ApiServerError("boom", status=500)
    c.fail["connections"] = ApiServerError("boom", status=500)
    snap = collector(c).collect()
    assert snap.last_sync is None
    assert snap.peer_connected is None
    assert snap.state is SyncState.OFFLINE  # 接続が確認できないものを「同期完了」とは言わない
    assert snap.errors


def test_collect_status_failure_is_error():
    c = make_client()
    c.fail["db_status"] = ApiServerError("boom", status=500)
    assert collector(c).collect().state is SyncState.ERROR


def test_collect_not_configured():
    c = make_client()
    assert StatusCollector(c, None, None).collect().state is SyncState.NOT_CONFIGURED
    del c.folders[FID]
    assert collector(c).collect().state is SyncState.NOT_CONFIGURED
    c = make_client()
    del c.devices[PEER]
    assert collector(c).collect().state is SyncState.NOT_CONFIGURED


def test_collect_folder_error_text_is_translated():
    c = make_client()
    c.status[FID].update({"state": "error", "error": "folder marker missing (this indicates ...)"})
    snap = collector(c).collect()
    assert snap.state is SyncState.ERROR and ".stfolder" in snap.message


def test_collect_conflicts():
    snap = collector(make_client(), conflict_counter=lambda: 3).collect()
    assert snap.state is SyncState.CONFLICT and snap.conflicts == 3

    def broken():
        raise OSError("disk")

    snap = collector(make_client(), conflict_counter=broken).collect()
    assert snap.conflicts is None and snap.state is SyncState.SYNCED


def test_collect_never_synced_folder_has_no_last_sync():
    c = make_client()
    c.folder_stats = {FID: {"lastFile": {"filename": "", "at": "0001-01-01T00:00:00Z"}}}
    snap = collector(c).collect()
    assert snap.last_sync is None and snap.last_file is None
    assert fm.fmt_time(snap.last_sync) == "—"


# ---------------------------------------------------------------- 表示
def test_formatting_uses_dash_for_unknown():
    assert fm.fmt_bytes(None) == fm.fmt_rate(None) == fm.fmt_percent(None) == fm.fmt_count(None) \
        == fm.fmt_time(None) == "—"
    assert fm.fmt_bytes(0) == "0 B"
    assert fm.fmt_bytes(1023) == "1023 B"
    assert fm.fmt_bytes(1536) == "1.5 KB"
    assert fm.fmt_bytes(1_800_000_000) == "1.7 GB"
    assert fm.fmt_rate(0.0) == "0 B/秒"
    assert fm.fmt_rate(2048.0) == "2.0 KB/秒"
    assert fm.fmt_percent(100.0) == "100%"
    assert fm.fmt_percent(99.99) == "99%"
    assert fm.fmt_percent(0) == "0%"
    assert fm.fmt_count(248, "") == "248"
    assert fm.fmt_count(1234) == "1,234 件"


def test_fmt_time_today_and_other_day():
    now = datetime(2026, 11, 21, 15, 0, 0)
    assert fm.fmt_time(datetime(2026, 11, 21, 12, 34, 56), now=now) == "12:34:56"
    assert fm.fmt_time(datetime(2026, 11, 20, 12, 34, 56), now=now) == "2026/11/20 12:34:56"


def test_my_id_constant_is_distinct():
    assert MY_ID != PEER


# ---------------------------------------------------------------- ファイルごとのエラーの説明
def test_file_error_text_translates_real_messages():
    from folderlink.core.sync_status import file_error_text

    # 実物（v2.1.7）の文面。tests/integration/test_edge_cases.py で取得
    case = ('syncing: finishing: checking existing file: remote "memo.txt" uses different upper or lowercase '
            'characters than local "Memo.txt"; change the casing on either side to match the other')
    locked = ("syncing: finishing: moving for conflict: removing item to be replaced: staggered versioner: "
              "archive: open locked.txt: The process cannot access the file because it is being used by "
              "another process.")
    assert "大文字と小文字" in file_error_text("memo.txt", case)
    assert "別のアプリで開かれている" in file_error_text("locked.txt", locked)
    assert "権限" in file_error_text("a", "open a: Access is denied.")
    assert file_error_text("x", "something else") == "「x」: something else"


def test_pull_errors_message_includes_first_reason():
    from folderlink.core.sync_status import decide_state

    state, message = decide_state(healthy(pull_errors=2, pull_error_detail="「a.txt」: 理由"))
    assert state is SyncState.ERROR and "2 件" in message and "「a.txt」: 理由" in message
