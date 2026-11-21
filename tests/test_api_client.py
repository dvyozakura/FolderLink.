"""api_client: 127.0.0.1 限定、タイムアウト、再試行、エラーの種類分け、API キーを出さない。"""

from __future__ import annotations

import io
import logging

import pytest
import requests

from fakes import FakeResponse, FakeSession, conn_error, timeout_error
from folderlink.core import api_client as ac
from folderlink.core.api_client import ApiClient
from folderlink.core.redact import RedactingFilter, redact

KEY = "sEcReT-key-0123456789-abcdefghijklmnopqrstuv"
PORT = 50001


def make(session: FakeSession, **kw) -> ApiClient:
    kw.setdefault("sleep", lambda s: None)
    return ApiClient(PORT, KEY, session=session, **kw)


# ---------------------------------------------------------------- 接続先の制限
@pytest.mark.parametrize("host", ["localhost", "0.0.0.0", "192.168.1.10", "example.com", "::1", "127.0.0.2"])
def test_only_loopback_ipv4_is_allowed(host):
    with pytest.raises(ValueError):
        ApiClient(PORT, KEY, host=host, session=FakeSession())


@pytest.mark.parametrize("port", [0, -1, 70000, "8384", None, True])
def test_bad_port_is_rejected(port):
    with pytest.raises(ValueError):
        ApiClient(port, KEY, session=FakeSession())


def test_empty_key_is_rejected():
    with pytest.raises(ValueError):
        ApiClient(PORT, "", session=FakeSession())


def test_requests_go_to_127_0_0_1_with_key_header_and_timeout():
    s = FakeSession({("GET", "/rest/system/ping"): {"ping": "pong"}})
    c = make(s)
    assert c.ping() is True
    call = s.calls[0]
    assert call["url"] == f"http://127.0.0.1:{PORT}/rest/system/ping"
    assert call["headers"] == {"X-API-Key": KEY}
    assert call["timeout"] == ac.DEFAULT_TIMEOUT
    assert call["allow_redirects"] is False


def test_real_session_ignores_proxy_environment():
    c = ApiClient(PORT, KEY)
    assert c._session.trust_env is False
    c.close()


def test_path_must_be_rest():
    with pytest.raises(ValueError):
        make(FakeSession()).request("GET", "/other")


# ---------------------------------------------------------------- エラーの種類分け
def test_connection_refused_becomes_connection_error_after_retries():
    s = FakeSession({("GET", "/rest/system/status"): conn_error()})
    c = make(s, retries=2)
    with pytest.raises(ac.ApiConnectionError):
        c.system_status()
    assert s.count("GET", "/rest/system/status") == 3  # 1回 + 再試行2回


def test_retry_then_success_with_backoff():
    sleeps: list[float] = []
    s = FakeSession({("GET", "/rest/system/status"): [conn_error(), conn_error(), {"myID": "ABC"}]})
    c = make(s, retries=2, backoff=0.5, sleep=sleeps.append)
    assert c.my_id() == "ABC"
    assert sleeps == [0.5, 1.0]


def test_timeout_is_its_own_kind_and_also_a_connection_error():
    s = FakeSession({("GET", "/rest/system/status"): timeout_error()})
    with pytest.raises(ac.ApiTimeoutError) as ei:
        make(s, retries=0).system_status()
    assert isinstance(ei.value, ac.ApiConnectionError)


def test_post_is_not_retried():
    s = FakeSession({("POST", "/rest/config/devices"): conn_error()})
    with pytest.raises(ac.ApiConnectionError):
        make(s, retries=3).add_device({"deviceID": "X"})
    assert s.count("POST", "/rest/config/devices") == 1


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure(status):
    s = FakeSession({("GET", "/rest/system/status"): FakeResponse(status, text="Not Authorized")})
    with pytest.raises(ac.ApiAuthError) as ei:
        make(s).system_status()
    assert ei.value.status == status
    assert s.count("GET", "/rest/system/status") == 1  # 認証失敗はやり直さない


def test_404_is_not_found():
    s = FakeSession({("GET", "/rest/config/folders/nope"): FakeResponse(404, text="no such folder")})
    with pytest.raises(ac.ApiNotFoundError):
        make(s).get_folder("nope")


@pytest.mark.parametrize("status,text", [
    (404, "folder is paused"),
    (500, "folder is paused"),
    (500, '{"error": "Folder paused"}'),
])
def test_paused_folder_is_its_own_kind(status, text):
    s = FakeSession({("GET", "/rest/db/completion"): FakeResponse(status, text=text)})
    with pytest.raises(ac.ApiFolderPausedError):
        make(s).db_completion("f1", "D1")


def test_400_is_bad_request():
    s = FakeSession({("POST", "/rest/config/folders"): FakeResponse(400, text="folder path empty")})
    with pytest.raises(ac.ApiBadRequestError) as ei:
        make(s).add_folder({})
    assert "folder path empty" in str(ei.value)


def test_500_is_server_error_and_not_retried():
    s = FakeSession({("GET", "/rest/db/status"): FakeResponse(500, text="boom")})
    with pytest.raises(ac.ApiServerError):
        make(s).db_status("f1")
    assert s.count("GET", "/rest/db/status") == 1


def test_503_is_retried():
    s = FakeSession({("GET", "/rest/db/status"): [FakeResponse(503, text="busy"), {"state": "idle"}]})
    assert make(s).db_status("f1") == {"state": "idle"}


def test_non_json_is_response_error():
    s = FakeSession({("GET", "/rest/system/status"): FakeResponse(200, text="<html>hello</html>")})
    with pytest.raises(ac.ApiResponseError):
        make(s).system_status()


def test_unexpected_shape_is_response_error():
    s = FakeSession({("GET", "/rest/config/folders"): {"not": "a list"}})
    with pytest.raises(ac.ApiResponseError):
        make(s).get_folders()


def test_redirect_is_not_followed_and_is_an_error():
    s = FakeSession({("GET", "/rest/system/status"): FakeResponse(302, text="")})
    with pytest.raises(ac.ApiResponseError):
        make(s).system_status()


def test_missing_my_id_is_response_error():
    s = FakeSession({("GET", "/rest/system/status"): {"uptime": 3}})
    with pytest.raises(ac.ApiResponseError):
        make(s).my_id()


def test_all_errors_share_base_class():
    for cls in (ac.ApiConnectionError, ac.ApiTimeoutError, ac.ApiAuthError, ac.ApiNotFoundError,
                ac.ApiFolderPausedError, ac.ApiBadRequestError, ac.ApiServerError, ac.ApiResponseError):
        assert issubclass(cls, ac.ApiError)


# ---------------------------------------------------------------- 呼び出しの形
def test_empty_body_returns_none_and_params_are_cleaned():
    s = FakeSession({("POST", "/rest/db/scan"): FakeResponse(200, text="")})
    make(s).scan("f1")
    assert s.last("POST", "/rest/db/scan")["params"] == {"folder": "f1"}


def test_device_id_is_url_quoted():
    s = FakeSession({("PATCH", "/rest/config/devices/A%2FB"): FakeResponse(200, text="")})
    make(s).patch_device("A/B", {"paused": True})
    assert s.last("PATCH", "/rest/config/devices/A%2FB")["json"] == {"paused": True}


def test_set_ignores_body():
    s = FakeSession({("POST", "/rest/db/ignores"): {"ignore": ["a"]}})
    make(s).set_ignores("f1", ["a"])
    call = s.last("POST", "/rest/db/ignores")
    assert call["json"] == {"ignore": ["a"]} and call["params"] == {"folder": "f1"}


def test_events_uses_long_read_timeout_and_no_retry():
    s = FakeSession({("GET", "/rest/events"): timeout_error()})
    with pytest.raises(ac.ApiTimeoutError):
        make(s, retries=5).events(since=7, timeout_s=30, events=["StateChanged", "ItemFinished"])
    assert s.count("GET", "/rest/events") == 1
    call = s.calls[0]
    assert call["params"] == {"since": 7, "timeout": 30, "events": "StateChanged,ItemFinished"}
    assert call["timeout"][1] == 40.0


def test_events_empty_is_list():
    s = FakeSession({("GET", "/rest/events"): FakeResponse(200, text="null")})
    assert make(s).events() == []


def test_pending_null_is_empty_dict():
    s = FakeSession({("GET", "/rest/cluster/pending/devices"): FakeResponse(200, text="null")})
    assert make(s).pending_devices() == {}


def test_restore_versions_returns_errors():
    s = FakeSession({("POST", "/rest/folder/versions"): {"a.txt": "no such version"}})
    assert make(s).restore_versions("f1", {"a.txt": "2026-01-01T00:00:00Z"}) == {"a.txt": "no such version"}


# ---------------------------------------------------------------- API キーを出さない
def test_repr_and_str_hide_key():
    c = make(FakeSession())
    assert KEY not in repr(c) and KEY not in str(c)
    assert KEY not in repr(vars(c).get("_session"))


def test_key_never_appears_in_exception_messages():
    """サーバーが応答に API キーをそのまま返してきても、例外の文面には出ない。"""
    responses = {
        ("GET", "/rest/system/status"): FakeResponse(500, text=f"bad key {KEY} given"),
        ("GET", "/rest/config/folders/x"): FakeResponse(404, text=f"X-API-Key: {KEY}"),
        ("POST", "/rest/config/folders"): FakeResponse(400, text=f"apikey={KEY}"),
        ("GET", "/rest/db/status"): FakeResponse(403, text=KEY),
        ("GET", "/rest/system/version"): requests.exceptions.ConnectionError(
            f"HTTPConnectionPool: url /rest?key={KEY} failed"),
        ("GET", "/rest/system/connections"): requests.exceptions.InvalidHeader(f"Invalid header {KEY}"),
    }
    c = make(FakeSession(responses), retries=0)
    calls = [c.system_status, lambda: c.get_folder("x"), lambda: c.add_folder({}),
             lambda: c.db_status("f"), c.system_version, c.connections]
    for call in calls:
        with pytest.raises(ac.ApiError) as ei:
            call()
        exc = ei.value
        for text in (str(exc), repr(exc), exc.detail, " ".join(map(str, exc.args))):
            assert KEY not in text
        # 元の例外（文面に URL や鍵を含み得る）を連鎖させていない
        assert exc.__cause__ is None


def test_key_never_appears_in_logs(caplog):
    """クライアントを使う一連の操作（成功・失敗）で、どのログにも API キーが出ない。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        s = FakeSession({
            ("GET", "/rest/system/status"): {"myID": "ABC"},
            ("GET", "/rest/db/status"): FakeResponse(500, text=f"oops {KEY}"),
        })
        c = make(s, retries=0)
        c.my_id()
        log = logging.getLogger("folderlink.test")
        try:
            c.db_status("f1")
        except ac.ApiError:
            log.exception("失敗しました client=%r", c)
        # わざと鍵を含む文字列をログに流しても、フィルターが消す
        log.info("header X-API-Key: %s", KEY)
        log.warning("raw %s", KEY)
        log.error("dict %s", {"X-API-Key": KEY, "api_key": KEY})
        try:
            raise RuntimeError(f"trace with {KEY}")
        except RuntimeError:
            log.exception("例外つき")
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    out = stream.getvalue()
    assert "失敗しました" in out and "例外つき" in out
    assert KEY not in out
    assert "***" in out
    # フィルターを通さない caplog 側でも、クライアント自身が出したログには鍵が無い
    for rec in caplog.records:
        if rec.name.startswith("folderlink.core"):
            assert KEY not in rec.getMessage()


def test_redact_patterns_for_unregistered_keys():
    assert "zzz999" not in redact("X-API-Key: zzz999")
    assert "tok123" not in redact("Authorization: Bearer tok123")
    assert "kkk" not in redact("STGUIAPIKEY=kkk next")
    assert "kkk" not in redact("syncthing serve --gui-apikey=kkk --no-browser")
    assert "inxml" not in redact("<gui><apikey>inxml</apikey></gui>")
    assert redact("普通の文章はそのまま") == "普通の文章はそのまま"
