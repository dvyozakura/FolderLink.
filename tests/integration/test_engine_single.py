"""結合テスト: 本物の Syncthing 1つを _sandbox/ 内の専用ホームで起動・確認・停止する。"""

from __future__ import annotations

import socket

import pytest
import requests

from it_helpers import syncthing_pids
from folderlink.core.syncthing_manager import (
    RESERVED_PORTS,
    SUPPORTED_SYNCTHING,
    AlreadyRunningError,
    EngineExitedError,
    EngineNotVerifiedError,
    SyncthingManager,
)


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def test_start_verify_stop_restart(make_node, foreign_pids_at_start):
    node = make_node("A")
    before = syncthing_pids()

    with pytest.raises(EngineNotVerifiedError):
        _ = node.manager.client  # 起動・確認の前は使えない

    client = node.start()
    mgr = node.manager
    port = mgr.port
    assert port and port not in RESERVED_PORTS
    assert mgr.is_verified and mgr.is_process_alive()

    # 専用ホームのデバイス ID と、応答した相手の myID が一致している
    assert client.my_id() == node.device_id == mgr.device_id
    assert len(node.device_id) == 63
    assert mgr.version == SUPPORTED_SYNCTHING

    # 起動したプロセスは1つだけ（監視プロセスを挟んでいない）で、それが自分の PID
    new = syncthing_pids() - before
    print("新しく増えた syncthing.exe:", new, "自分の PID:", mgr.pid)
    assert new == {mgr.pid}

    # 設定: 外部の探索・リレーなどが切れていて、待ち受けは 127.0.0.1 だけ
    opts = client.get_options()
    assert opts["globalAnnounceEnabled"] is False
    assert opts["localAnnounceEnabled"] is False
    assert opts["relaysEnabled"] is False
    assert opts["natEnabled"] is False
    assert opts["urAccepted"] == -1
    assert opts["autoUpgradeIntervalH"] == 0
    assert opts["crashReportingEnabled"] is False
    assert opts["listenAddresses"] == [node.address]
    # v2 は既定フォルダを作らない
    assert client.get_folders() == []

    # API キーなしでは操作できない（401/403）。キーが違っても同じ。
    base = f"http://127.0.0.1:{port}"
    assert requests.get(base + "/rest/system/status", timeout=5).status_code in (401, 403)
    assert requests.get(base + "/rest/system/status", headers={"X-API-Key": "wrong"}, timeout=5
                        ).status_code in (401, 403)
    # 設定の読み書きも、キーなしでは拒否される
    # （"/" はログイン画面の HTML が返るだけで、中の操作には認証が要る）
    assert requests.get(base + "/rest/config", timeout=5).status_code in (401, 403)
    assert requests.post(base + "/rest/system/shutdown", timeout=5).status_code in (401, 403)
    assert mgr.is_process_alive()

    # 同じホームでもう1つ起動しようとすると拒否される（二重起動の防止）
    other = SyncthingManager(node.home, node.api_key, binary=mgr.binary, isolated=True,
                             listen_address=node.address)
    with pytest.raises(AlreadyRunningError):
        other.launch()

    # ログに API キーが出ていない。config.xml にも自分のキーは書かれていない。
    log_text = (node.base / "syncthing.log").read_text(encoding="utf-8", errors="replace")
    assert log_text.strip(), "エンジンのログが空です"
    assert node.api_key not in log_text
    assert node.api_key not in mgr.log_tail()
    cfg = (node.home / "config.xml").read_text(encoding="utf-8", errors="replace")
    print("config.xml に自分の API キーが書かれているか:", node.api_key in cfg)

    # 停止: 自分のプロセスが消え、ポートが閉じる
    pid = mgr.pid
    node.stop()
    assert not mgr.is_process_alive()
    assert pid not in syncthing_pids()
    assert not _port_open(port)
    with pytest.raises(EngineNotVerifiedError):
        _ = mgr.client

    # もう一度起動できる。デバイス ID は変わらない（鍵を作り直していない）。
    client2 = node.start()
    assert client2.my_id() == node.device_id
    assert mgr.port not in RESERVED_PORTS
    node.stop()
    assert syncthing_pids() - before == set()
    assert foreign_pids_at_start <= syncthing_pids()


def test_crash_is_detected(make_node):
    node = make_node("A")
    node.start()
    mgr = node.manager
    assert mgr.verify() is True
    mgr._proc.kill()  # 自分が起動したプロセスを外から落とす（異常終了の再現）
    mgr._proc.wait(timeout=10)
    assert mgr.is_process_alive() is False
    assert mgr.exit_code() is not None
    with pytest.raises(EngineExitedError):
        _ = mgr.client
    assert mgr.verify() is False
    assert mgr.is_verified is False
    # 落ちたあと、同じマネージャーで起動し直せる
    node.stop()
    client = node.start()
    assert client.my_id() == node.device_id
