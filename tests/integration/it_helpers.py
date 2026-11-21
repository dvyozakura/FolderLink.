"""結合テスト用の道具（本物の Syncthing を _sandbox/ 内で動かす）。

安全ルール:
- 同期するフォルダも Syncthing のホームも、プロジェクト内の _sandbox/ の下だけ。
- 既定ポート（8384 / 22000）は使わない。空きポートを自動で選び、127.0.0.1 だけで待ち受ける。
- 外部の探索・リレー・NAT 越えは切る（isolated=True）。相手のアドレスは直接指定する。
- 止めるのは、このテストが起動したプロセスだけ。
"""

from __future__ import annotations

import csv
import hashlib
import io
import shutil
import subprocess
import time
from pathlib import Path

from folderlink.core import ignore_presets
from folderlink.core.config_store import generate_api_key
from folderlink.core.pairing import Pairing, generate_folder_id
from folderlink.core.syncthing_manager import RESERVED_PORTS, SyncthingManager, find_free_port

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SANDBOX = PROJECT_ROOT / "_sandbox"
IT_ROOT = SANDBOX / "it"
BINARY = PROJECT_ROOT / "vendor" / "syncthing" / "syncthing.exe"

_HIDDEN = (".stfolder", ".stversions", ".stignore")


def safe_rmtree(path: Path) -> None:
    """_sandbox/ の下だけを消す。それ以外を指していたら例外（安全ルール）。"""
    path = Path(path).resolve()
    sandbox = SANDBOX.resolve()
    if sandbox not in path.parents:
        raise RuntimeError(f"_sandbox の外は消しません: {path}")
    if not path.exists():
        return

    def _onerror(func, p, exc_info):
        try:
            Path(p).chmod(0o700)
            func(p)
        except OSError:
            pass

    for _ in range(5):
        shutil.rmtree(path, onerror=_onerror)
        if not path.exists():
            return
        time.sleep(0.5)


def make_dir(name: str) -> Path:
    d = IT_ROOT / name
    safe_rmtree(d)
    d.mkdir(parents=True)
    return d


def syncthing_pids() -> set[int]:
    """いま動いている syncthing.exe の PID（見るだけ）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq syncthing.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=30,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    pids: set[int] = set()
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 2 and row[0].lower() == "syncthing.exe" and row[1].isdigit():
            pids.add(int(row[1]))
    return pids


def wait_until(pred, timeout: float = 60.0, interval: float = 0.25, desc: str = "条件"):
    """pred() が真になるまで待つ。時間切れなら AssertionError。真になった値を返す。"""
    deadline = time.monotonic() + timeout
    last_exc = None
    while time.monotonic() < deadline:
        try:
            value = pred()
            if value:
                return value
        except Exception as exc:  # 途中の一時的な失敗は待ち続ける
            last_exc = exc
        time.sleep(interval)
    extra = f" / 最後の例外: {last_exc!r}" if last_exc else ""
    raise AssertionError(f"時間切れ（{timeout:.0f} 秒）: {desc}{extra}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def tree(folder: Path, include_conflicts: bool = True) -> dict[str, str]:
    """同期フォルダの中身。相対パス → sha256（Syncthing の管理用ファイルと一時ファイルは除く）。"""
    result: dict[str, str] = {}
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder).as_posix()
        top = rel.split("/")[0]
        if top in _HIDDEN or p.name.startswith("~syncthing~") or p.name.startswith(".syncthing."):
            continue
        if not include_conflicts and ".sync-conflict-" in p.name:
            continue
        try:
            if p.is_file():
                result[rel] = sha256_file(p)
            elif p.is_dir():
                result[rel + "/"] = "dir"
        except OSError:
            result[rel] = "unreadable"
    return result


class Node:
    """テスト用の1台分（ホーム・同期フォルダ・エンジン）。"""

    def __init__(self, name: str, base: Path, binary: Path) -> None:
        self.name = name
        self.base = base / name
        self.home = self.base / "home"
        self.folder = self.base / "sync"
        self.folder.mkdir(parents=True)
        self.api_key = generate_api_key()
        self.sync_port = find_free_port()
        assert self.sync_port not in RESERVED_PORTS
        self.manager = SyncthingManager(
            self.home,
            self.api_key,
            binary=binary,
            log_path=self.base / "syncthing.log",
            isolated=True,
            listen_address=f"tcp://127.0.0.1:{self.sync_port}",
        )
        self.device_id = ""

    @property
    def address(self) -> str:
        return f"tcp://127.0.0.1:{self.sync_port}"

    @property
    def client(self):
        return self.manager.client

    def start(self):
        self.device_id = self.manager.ensure_home()
        self.manager.start(timeout=120)
        return self.manager.client

    def stop(self) -> None:
        self.manager.stop()


class Pair:
    """FolderLink のコード（Pairing）でペアリングと共有を済ませた A・B。"""

    def __init__(self, base: Path, binary: Path, presets: list[str] | None = None,
                 start_paused: bool = False) -> None:
        self.a = Node("A", base, binary)
        self.b = Node("B", base, binary)
        self.nodes = [self.a, self.b]
        self.folder_id = generate_folder_id()
        self.presets = list(presets or [])
        self.start_paused = start_paused
        self.pa: Pairing
        self.pb: Pairing

    def start(self) -> "Pair":
        for node in self.nodes:
            node.start()
            # テストを速くするための設定（再接続の間隔を短く）
            node.client.patch_options({"reconnectionIntervalS": 3})
        self.rebind()
        # 外部の探索は切ってあるので、相手のアドレスを直接指定する
        self.pa.add_device(self.b.device_id, "PC-B", addresses=[self.b.address])
        self.pb.add_device(self.a.device_id, "PC-A", addresses=[self.a.address])
        for node, pairing, other in ((self.a, self.pa, self.b), (self.b, self.pb, self.a)):
            if self.presets:
                ignore_presets.apply_to_folder(node.folder, self.presets)
            pairing.share_folder(self.folder_id, node.folder, other.device_id, label="共有テスト",
                                 paused=self.start_paused)
            node.client.patch_folder(self.folder_id, {"fsWatcherDelayS": 1})
        return self

    def other(self, node: Node) -> Node:
        return self.b if node is self.a else self.a

    def rebind(self) -> None:
        """（再）起動したエンジンのクライアントに Pairing を付け直す。"""
        self.pa = Pairing(self.a.client, self.a.device_id)
        self.pb = Pairing(self.b.client, self.b.device_id)

    def connected(self) -> bool:
        return self.pa.peer_state(self.b.device_id).connected and self.pb.peer_state(self.a.device_id).connected

    def wait_connected(self, timeout: float = 60.0) -> None:
        wait_until(self.connected, timeout, desc="A と B の接続")

    def in_sync(self, node: Node) -> bool:
        other = self.other(node)
        st = node.client.db_status(self.folder_id)
        if st.get("state") != "idle" or st.get("needTotalItems") != 0 or st.get("needBytes") != 0:
            return False
        comp = node.client.db_completion(self.folder_id, other.device_id)
        return (comp.get("completion") == 100 and comp.get("needItems") == 0
                and comp.get("needDeletes") == 0 and comp.get("remoteState") == "valid")

    def scan(self, *nodes: Node) -> None:
        for node in nodes or self.nodes:
            node.client.scan(self.folder_id)

    def wait_synced(self, timeout: float = 90.0) -> None:
        wait_until(lambda: self.in_sync(self.a) and self.in_sync(self.b), timeout, desc="両側の同期完了")

    def wait_trees_equal(self, timeout: float = 90.0) -> dict[str, str]:
        def same():
            ta, tb = tree(self.a.folder), tree(self.b.folder)
            return (ta,) if ta == tb else None

        try:
            return wait_until(same, timeout, desc="A と B のフォルダ内容の一致")[0]
        except AssertionError as exc:
            ta, tb = tree(self.a.folder), tree(self.b.folder)
            only_a = sorted(set(ta) - set(tb))[:10]
            only_b = sorted(set(tb) - set(ta))[:10]
            diff = sorted(k for k in set(ta) & set(tb) if ta[k] != tb[k])[:10]
            raise AssertionError(f"{exc} / Aだけ: {only_a} / Bだけ: {only_b} / 内容違い: {diff}") from None

    def settle(self, timeout: float = 90.0) -> dict[str, str]:
        """両側を再スキャンし、同期完了と内容の一致を待つ。一致した内容を返す。"""
        self.scan()
        result = self.wait_trees_equal(timeout)
        self.wait_synced(timeout)
        return result

    def stop(self) -> None:
        for node in self.nodes:
            try:
                node.stop()
            except Exception:
                pass
