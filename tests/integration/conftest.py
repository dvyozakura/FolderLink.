"""結合テスト（本物の Syncthing を起動する）のフィクスチャ。道具は it_helpers.py。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from it_helpers import BINARY, Node, Pair, make_dir, syncthing_pids


def pytest_collection_modifyitems(items):
    for item in items:
        if "integration" in str(item.fspath).replace("\\", "/").split("/"):
            item.add_marker(pytest.mark.integration)


def _safe_name(text: str, limit: int = 50) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:limit]


@pytest.fixture(scope="session")
def binary() -> Path:
    if not BINARY.is_file():
        pytest.skip("vendor/syncthing/syncthing.exe がありません（./Tools/fetch_syncthing.sh を実行）")
    return BINARY


@pytest.fixture(scope="session")
def foreign_pids_at_start() -> set[int]:
    """テスト開始前から動いていた syncthing.exe（利用者のものかもしれない。絶対に触らない）。"""
    return syncthing_pids()


@pytest.fixture
def workdir(request) -> Path:
    return make_dir(_safe_name(request.node.name))


@pytest.fixture
def make_node(workdir, binary, foreign_pids_at_start):
    nodes: list[Node] = []

    def _make(name: str) -> Node:
        node = Node(name, workdir, binary)
        nodes.append(node)
        return node

    yield _make
    for node in nodes:
        try:
            node.stop()
        except Exception:
            pass
    # 利用者のものかもしれないプロセスが、テストの前後で減っていないこと
    assert foreign_pids_at_start <= syncthing_pids(), "テスト前からあった syncthing.exe が消えています"


@pytest.fixture(scope="module")
def shared_pair(request, binary, foreign_pids_at_start):
    """モジュール内で使い回す A・B（Unity と OS の除外プリセットつき）。"""
    pair = Pair(make_dir("shared_" + _safe_name(request.module.__name__, 40)), binary,
                presets=["unity", "os_junk"])
    try:
        pair.start()
        pair.wait_connected()
        pair.wait_synced()
        yield pair
    finally:
        pair.stop()
        assert foreign_pids_at_start <= syncthing_pids()


@pytest.fixture
def new_pair(workdir, binary, foreign_pids_at_start):
    """テストごとに作る A・B。"""
    pairs: list[Pair] = []

    def _make(**kw) -> Pair:
        pair = Pair(workdir, binary, **kw)
        pairs.append(pair)
        return pair

    yield _make
    for pair in pairs:
        pair.stop()
    assert foreign_pids_at_start <= syncthing_pids()
