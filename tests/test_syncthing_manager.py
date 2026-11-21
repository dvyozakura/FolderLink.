"""syncthing_manager: 専用ホーム、空きポート、起動・停止・異常終了、二重起動、誤接続の防止。

本物の Syncthing は起動しない（プロセスも HTTP も偽物）。本物での確認は tests/integration/。
"""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET

import pytest

from fake_engine import OTHER_ID, OWN_ID, SAMPLE_CONFIG_XML, FakeEngine, FakeProc, FakeRunner
from folderlink.core import syncthing_manager as sm
from folderlink.core.api_client import ApiClient, ApiConnectionError
from folderlink.core.config_store import ConfigStore, RuntimeInfo
from folderlink.core.syncthing_manager import SyncthingManager

KEY = "unit-test-api-key-0123456789abcdefghijklmnop"


class Harness:
    def __init__(self, tmp_path, port: int | None = None, **kw):
        self.binary = tmp_path / "syncthing.exe"
        self.binary.write_bytes(b"not a real exe")
        self.home = tmp_path / "home"
        self.store = ConfigStore(tmp_path / "data")
        self.runner = FakeRunner()
        self.procs: list[FakeProc] = []
        self.client_ports: list[int] = []

        def popen(args, **kwargs):
            proc = FakeProc(args, **kwargs)
            self.procs.append(proc)
            return proc

        def client_factory(p, key, **ckw):
            self.client_ports.append(p)
            return ApiClient(p, key, **{**ckw, "retries": 0})

        self.port = port
        kw.setdefault("client_factory", client_factory)
        self.mgr = SyncthingManager(
            self.home, KEY, binary=self.binary, store=self.store, popen=popen, runner=self.runner,
            port_picker=lambda: self.port if self.port is not None else sm.find_free_port(), **kw,
        )

    @property
    def proc(self) -> FakeProc:
        return self.procs[-1]


@pytest.fixture
def own_engine():
    with FakeEngine(my_id=OWN_ID, required_key=KEY) as e:
        yield e


# ---------------------------------------------------------------- ポート
def test_find_free_port_never_returns_reserved_ports():
    assert {8384, 22000} <= sm.RESERVED_PORTS
    for _ in range(20):
        assert sm.find_free_port() not in sm.RESERVED_PORTS


def test_find_free_port_gives_up_when_everything_is_reserved():
    with pytest.raises(sm.EngineError):
        sm.find_free_port(avoid=frozenset(range(1, 65536)), attempts=3)


def test_launch_refuses_reserved_port(tmp_path):
    h = Harness(tmp_path, port=8384)
    with pytest.raises(sm.EngineError):
        h.mgr.launch()
    assert h.procs == []  # 何も起動していない
    assert 8384 not in h.client_ports  # 8384 には一度も接続を試みていない


# ---------------------------------------------------------------- 専用ホーム
def test_ensure_home_generates_once_and_reads_device_id(tmp_path):
    h = Harness(tmp_path)
    assert h.mgr.ensure_home() == OWN_ID
    assert h.runner.count("generate") == 1
    gen = next(c for c in h.runner.calls if "generate" in c["args"])
    assert f"--home={h.home}" in gen["args"]
    # GUI のパスワードは標準入力で渡し、コマンドラインには書かない
    assert "--gui-password=-" in gen["args"]
    assert gen["input"].strip() and gen["input"].strip() not in " ".join(gen["args"])
    # 2回目は鍵を作り直さない
    assert h.mgr.ensure_home() == OWN_ID
    assert h.runner.count("generate") == 1


def test_tool_environment_is_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("STHOMEDIR", r"C:\somewhere\real\Syncthing")
    monkeypatch.setenv("STGUIADDRESS", "0.0.0.0:8384")
    monkeypatch.setenv("STGUIAPIKEY", "inherited-key")
    monkeypatch.setenv("STEAM_UNRELATED", "keep")
    h = Harness(tmp_path)
    h.mgr.ensure_home()
    for call in h.runner.calls:
        env = call["env"]
        assert "STHOMEDIR" not in env and "STGUIADDRESS" not in env
        assert "STGUIAPIKEY" not in env  # 準備用のコマンドにはキーを渡さない
        assert env["STEAM_UNRELATED"] == "keep"
        assert KEY not in " ".join(call["args"])


def test_missing_binary(tmp_path):
    h = Harness(tmp_path)
    h.binary.unlink()
    with pytest.raises(sm.BinaryMissingError):
        h.mgr.ensure_home()
    with pytest.raises(sm.BinaryMissingError):
        h.mgr.launch()


def test_unreadable_device_id(tmp_path):
    h = Harness(tmp_path)
    h.runner.device_id_output = "something went wrong"
    with pytest.raises(sm.HomeSetupError):
        h.mgr.ensure_home()


def test_prepare_config_isolated(tmp_path):
    (tmp_path / "config.xml").write_text(SAMPLE_CONFIG_XML, encoding="utf-8")
    sm.prepare_config(tmp_path, isolated=True, listen_address="tcp://127.0.0.1:50999")
    opts = ET.parse(tmp_path / "config.xml").getroot().find("options")
    get = lambda n: opts.find(n).text  # noqa: E731
    assert get("globalAnnounceEnabled") == "false"
    assert get("localAnnounceEnabled") == "false"
    assert get("relaysEnabled") == "false"
    assert get("natEnabled") == "false"
    assert get("urAccepted") == "-1"
    assert get("autoUpgradeIntervalH") == "0"
    assert get("crashReportingEnabled") == "false"  # 無かった項目は追加される
    assert get("startBrowser") == "false"
    assert [e.text for e in opts.findall("listenAddress")] == ["tcp://127.0.0.1:50999"]


def test_prepare_config_product_keeps_discovery(tmp_path):
    (tmp_path / "config.xml").write_text(SAMPLE_CONFIG_XML, encoding="utf-8")
    sm.prepare_config(tmp_path)
    opts = ET.parse(tmp_path / "config.xml").getroot().find("options")
    assert opts.find("globalAnnounceEnabled").text == "true"  # 製品版は探索・リレーを使う
    assert opts.find("relaysEnabled").text == "true"
    assert opts.find("listenAddress").text == "default"
    assert opts.find("urAccepted").text == "-1"
    assert opts.find("autoUpgradeIntervalH").text == "0"


def test_prepare_config_broken_xml(tmp_path):
    (tmp_path / "config.xml").write_text("<configuration", encoding="utf-8")
    with pytest.raises(sm.HomeSetupError):
        sm.prepare_config(tmp_path)


# ---------------------------------------------------------------- 起動
def test_client_is_unavailable_before_verification(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(sm.EngineNotVerifiedError):
        _ = h.mgr.client


def test_start_with_own_engine(tmp_path, own_engine):
    h = Harness(tmp_path, port=own_engine.port)
    client = h.mgr.start(timeout=10)
    assert h.mgr.is_verified and client is h.mgr.client
    assert client.my_id() == OWN_ID
    assert h.mgr.version == "v2.1.7"
    # 起動時のコマンドライン
    args = h.proc.args
    assert args[1] == "serve"
    assert f"--gui-address=127.0.0.1:{own_engine.port}" in args
    for flag in ("--no-browser", "--no-console", "--no-restart", "--no-upgrade"):
        assert flag in args
    assert f"--home={h.home}" in args
    # API キーはコマンドラインに無く、環境変数で渡している
    assert KEY not in " ".join(args)
    env = h.proc.kwargs["env"]
    assert env["STGUIAPIKEY"] == KEY
    assert env["STMONITORED"] == "yes"
    assert env["STNOUPGRADE"] == "1"
    # 実行時情報が保存されている（機密は含まない）
    info = h.store.load_runtime()
    assert info.port == own_engine.port and info.pid == h.proc.pid and info.device_id == OWN_ID
    assert KEY not in h.store.runtime_path.read_text(encoding="utf-8")
    assert KEY not in repr(h.mgr)
    h.mgr.stop(timeout=0.1)


def test_inherited_syncthing_env_is_not_passed_to_engine(tmp_path, own_engine, monkeypatch):
    monkeypatch.setenv("STHOMEDIR", r"C:\real\Syncthing")
    monkeypatch.setenv("STGUIADDRESS", "0.0.0.0:8384")
    h = Harness(tmp_path, port=own_engine.port)
    h.mgr.start(timeout=10)
    env = h.proc.kwargs["env"]
    assert "STHOMEDIR" not in env and "STGUIADDRESS" not in env
    h.mgr.stop(timeout=0.1)


def test_foreign_engine_with_different_id_is_refused_and_untouched(tmp_path):
    """別の Syncthing が（どんな API キーでも受け付ける最悪の形で）応答した場合。"""
    with FakeEngine(my_id=OTHER_ID, required_key=None) as foreign:
        h = Harness(tmp_path, port=foreign.port)
        with pytest.raises(sm.ForeignEngineError):
            h.mgr.start(timeout=10)
        # 相手に送ったのは状態の問い合わせだけ。設定変更も停止も送っていない。
        assert foreign.writes() == []
        assert set(foreign.requests) == {("GET", "/rest/system/status")}
        # 操作用のクライアントは渡されない
        assert not h.mgr.is_verified
        with pytest.raises(sm.EngineNotVerifiedError):
            _ = h.mgr.client
        # 自分が起動したプロセスだけを止めた
        assert h.proc.terminated
        assert h.store.load_runtime() == RuntimeInfo()
        # stop() を呼んでも、相手には何も送らない
        h.mgr.stop()
        assert foreign.writes() == []
        # 相手は生きたまま（影響を与えていない）
        assert ApiClient(foreign.port, "any-key-at-all-0123456789").my_id() == OTHER_ID


def test_foreign_engine_rejecting_our_key_is_refused_and_untouched(tmp_path):
    with FakeEngine(my_id=OTHER_ID, required_key="the-users-own-key") as foreign:
        h = Harness(tmp_path, port=foreign.port)
        with pytest.raises(sm.ForeignEngineError):
            h.mgr.start(timeout=10)
        assert foreign.writes() == []
        assert h.proc.terminated
        assert not h.mgr.is_verified


def test_engine_that_exits_immediately(tmp_path):
    h = Harness(tmp_path)
    h.mgr.launch()
    h.proc.exit(1)
    with pytest.raises(sm.EngineExitedError) as ei:
        h.mgr.poll_ready()
    assert ei.value.exit_code == 1
    assert not h.mgr.is_verified


def test_start_can_be_cancelled_quickly(tmp_path):
    h = Harness(tmp_path)  # 誰も待ち受けていないポート → いつまでも準備完了にならない
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    t0 = time.monotonic()
    with pytest.raises(sm.StartCancelled):
        h.mgr.start(cancel=cancel, timeout=60)
    assert time.monotonic() - t0 < 10
    assert h.proc.terminated
    assert not h.mgr.is_process_alive()


def test_start_times_out(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(sm.EngineStartTimeout):
        h.mgr.start(timeout=0.5, interval=0.05)
    assert h.proc.terminated


def test_waiting_does_not_use_time_sleep(tmp_path, monkeypatch):
    """起動待ちは中断可能な待ち（Event.wait）で行い、time.sleep を使わない。"""
    answers = [ApiConnectionError("x"), ApiConnectionError("x"), ApiConnectionError("x")]

    class SlowClient:
        def __init__(self, *a, **k):
            pass

        def my_id(self):
            if answers:
                raise answers.pop(0)
            return OWN_ID

        def system_version(self):
            return {"version": "v2.1.7"}

        def close(self):
            pass

    def no_sleep(_seconds):
        raise AssertionError("time.sleep が呼ばれました")

    h = Harness(tmp_path, client_factory=SlowClient)
    monkeypatch.setattr(time, "sleep", no_sleep)
    h.mgr.start(timeout=10, interval=0.01)
    assert h.mgr.is_verified and answers == []


def test_poll_ready_is_a_single_quick_step(tmp_path):
    h = Harness(tmp_path)
    h.mgr.launch()
    t0 = time.monotonic()
    assert h.mgr.poll_ready() is False  # まだ。例外にもならない
    assert time.monotonic() - t0 < 5
    h.mgr.stop()


# ---------------------------------------------------------------- 二重起動
def test_second_manager_on_same_home_is_refused(tmp_path, own_engine):
    h1 = Harness(tmp_path, port=own_engine.port)
    h1.mgr.start(timeout=10)
    h2 = Harness(tmp_path, port=own_engine.port)
    with pytest.raises(sm.AlreadyRunningError):
        h2.mgr.launch()
    assert h2.procs == []
    with pytest.raises(sm.AlreadyRunningError):
        h1.mgr.launch()  # 同じマネージャーで2回目も不可
    assert len(h1.procs) == 1
    h1.mgr.stop(timeout=0.1)
    # 1つ目を止めたあとは起動できる
    h2.mgr.launch()
    h2.mgr.stop()


# ---------------------------------------------------------------- 停止・異常終了
def test_stop_asks_own_engine_to_shut_down(tmp_path, own_engine):
    h = Harness(tmp_path, port=own_engine.port)
    h.mgr.start(timeout=10)
    own_engine.on_shutdown = lambda: h.proc.exit(0)
    h.mgr.stop(timeout=5)
    assert ("POST", "/rest/system/shutdown") in own_engine.requests
    assert h.proc.returncode == 0 and not h.proc.terminated  # 正常終了できたので強制終了していない
    assert h.store.load_runtime() == RuntimeInfo()
    with pytest.raises(sm.EngineNotVerifiedError):
        _ = h.mgr.client


def test_stop_terminates_own_process_if_shutdown_is_ignored(tmp_path, own_engine):
    h = Harness(tmp_path, port=own_engine.port)
    h.mgr.start(timeout=10)
    h.mgr.stop(timeout=0.2)
    assert h.proc.terminated
    h.mgr.stop()  # 2回呼んでも落ちない


def test_crash_after_start_is_detected(tmp_path, own_engine):
    h = Harness(tmp_path, port=own_engine.port)
    h.mgr.start(timeout=10)
    h.proc.exit(2)
    assert h.mgr.is_process_alive() is False and h.mgr.exit_code() == 2
    with pytest.raises(sm.EngineExitedError):
        _ = h.mgr.client
    assert h.mgr.verify() is False


def test_verify_detects_identity_change(tmp_path, own_engine):
    h = Harness(tmp_path, port=own_engine.port)
    h.mgr.start(timeout=10)
    assert h.mgr.verify() is True
    own_engine.my_id = OTHER_ID  # 同じポートの相手が別物に入れ替わった
    assert h.mgr.verify() is False
    with pytest.raises(sm.EngineNotVerifiedError):
        _ = h.mgr.client
    h.mgr.stop(timeout=0.1)
    assert ("POST", "/rest/system/shutdown") not in own_engine.requests  # 別物には停止を送らない


# ---------------------------------------------------------------- 前回の残り
def test_orphan_of_our_own_is_shut_down(tmp_path, own_engine):
    h = Harness(tmp_path)
    h.store.save_runtime(RuntimeInfo(pid=4242, port=own_engine.port, device_id=OWN_ID))
    answered = {"n": 0}

    def after_shutdown():
        own_engine.required_key = "gone"  # 以後は応答を拒否（=終了した扱い）

    own_engine.on_shutdown = after_shutdown
    h.mgr.launch()
    assert ("POST", "/rest/system/shutdown") in own_engine.requests
    assert h.store.load_runtime() == RuntimeInfo()
    assert answered["n"] == 0
    h.mgr.stop()


def test_orphan_record_pointing_at_foreign_engine_is_left_alone(tmp_path):
    with FakeEngine(my_id=OTHER_ID, required_key=None) as foreign:
        h = Harness(tmp_path)
        h.store.save_runtime(RuntimeInfo(pid=4242, port=foreign.port, device_id=OWN_ID))
        h.mgr.launch()
        assert foreign.writes() == []  # 停止も設定変更も送っていない
        h.mgr.stop()
        assert foreign.writes() == []


def test_orphan_record_with_reserved_port_is_never_contacted(tmp_path):
    h = Harness(tmp_path)
    h.store.save_runtime(RuntimeInfo(pid=4242, port=8384, device_id=OWN_ID))
    h.mgr.launch()
    assert 8384 not in h.client_ports
    assert h.store.load_runtime() == RuntimeInfo()
    h.mgr.stop()


def test_log_tail_is_redacted(tmp_path):
    h = Harness(tmp_path)
    h.mgr.log_path.parent.mkdir(parents=True, exist_ok=True)
    h.mgr.log_path.write_text(f"line1\nkey is {KEY}\nline3\n", encoding="utf-8")
    tail = h.mgr.log_tail(5)
    assert "line3" in tail and KEY not in tail
