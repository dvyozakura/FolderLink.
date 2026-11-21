"""Syncthing（同期エンジン）のプロセス管理。

守ること（仕様 2章・6章-1、CLAUDE.md 技術メモ）:
- 専用ホーム（設定・鍵・データベース）だけを使う。
- REST API は 127.0.0.1 の、自分で選んだ空きポートで待ち受けさせる。既定の 8384 は使わない。
- API キーは自分で作ったランダムな値を環境変数で渡す（コマンドラインに書かない）。
- **誤接続の防止**: 「自分が起動したプロセスが生きている」かつ「/rest/system/status の myID が
  専用ホームのデバイス ID と一致する」ことを確かめるまで、API クライアントを外に渡さない。
  確かめられない相手には GET /rest/system/status 以外を一切送らない。
- 止めるのは自分が起動したプロセスだけ。
- 起動待ちは小刻みな確認と中断可能な待ち（threading.Event）で行い、time.sleep は使わない。
  `launch()`（鍵の生成などで数秒かかり得る）・`start()` はワーカースレッド（QThread）から呼ぶ。
  `poll_ready()` は1回分の確認だけをして、すぐ戻る。
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from folderlink.core.api_client import (
    ApiAuthError,
    ApiClient,
    ApiConnectionError,
    ApiError,
)
from folderlink.core.config_store import ConfigStore, RuntimeInfo
from folderlink.core.redact import redact, register_secret

log = logging.getLogger(__name__)

#: 動作を確認した Syncthing のバージョン（Tools/fetch_syncthing.sh と合わせる）
SUPPORTED_SYNCTHING = "v2.1.7"

#: 絶対に使わないポート（利用者が別に動かしている Syncthing の既定ポート）
RESERVED_PORTS = frozenset({8384, 22000, 21027})

LOCK_NAME = "folderlink.lock"
LOG_ROTATE_BYTES = 5 * 1024 * 1024

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200


# ---------------------------------------------------------------- 例外
class EngineError(Exception):
    """エンジンの起動・停止の失敗（基底）。文面は利用者に見せてよい日本語。"""

    def __init__(self, message: str) -> None:
        super().__init__(redact(message))


class BinaryMissingError(EngineError):
    """syncthing.exe が無い。"""


class HomeSetupError(EngineError):
    """専用ホームの作成・デバイス ID の取得に失敗。"""


class AlreadyRunningError(EngineError):
    """同じホームを使う FolderLink が既に動いている。"""


class EngineExitedError(EngineError):
    """エンジンが起動直後に終了した／動作中に落ちた。"""

    def __init__(self, message: str, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class ForeignEngineError(EngineError):
    """応答した相手が、自分のエンジンではない（別の Syncthing など）。"""


class EngineStartTimeout(EngineError):
    """時間内に起動が確認できなかった。"""


class StartCancelled(EngineError):
    """起動待ちが中断された。"""


class EngineNotVerifiedError(EngineError):
    """本人確認が済んでいないエンジンを操作しようとした。"""


# ---------------------------------------------------------------- 補助
def find_free_port(avoid: frozenset[int] | set[int] = RESERVED_PORTS, attempts: int = 50) -> int:
    """127.0.0.1 で空いている TCP ポートを OS に選ばせる。予約済みの番号は避ける。"""
    for _ in range(attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in avoid:
            return port
    raise EngineError("空いているポートを見つけられませんでした。")


def default_binary_path() -> Path:
    """syncthing.exe の既定の場所。

    - EXE 化した製品版: FolderLink.exe と同じ場所の syncthing\\syncthing.exe
    - 開発中: プロジェクトの vendor\\syncthing\\syncthing.exe
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "syncthing" / "syncthing.exe"
    return Path(__file__).resolve().parents[3] / "vendor" / "syncthing" / "syncthing.exe"


class _FileLock:
    """ホームごとの排他（同じホームで2つ動かさない）。プロセスが死ねば OS が解放する。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None

    def acquire(self) -> bool:
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - Windows 専用アプリだが、開発用に用意
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            fh.close()


class _KillOnCloseJob:
    """Windows のジョブオブジェクト。FolderLink が（異常終了を含めて）終わったら、
    自分が起動したエンジンとその子プロセスも必ず終わるようにする保険。

    対象は、このジョブに入れたプロセス（=自分が起動したもの）とその子だけ。
    """

    def __init__(self) -> None:
        self._handle: Any = None

    def assign(self, process_handle: int) -> bool:
        if os.name != "nt":
            return False
        try:
            import ctypes
            from ctypes import wintypes

            class BASIC(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class EXT(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BASIC),
                    ("IoInfo", IO),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.SetInformationJobObject.restype = wintypes.BOOL
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = [wintypes.HANDLE]

            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                return False
            info = EXT()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                k32.CloseHandle(handle)
                return False
            if not k32.AssignProcessToJobObject(handle, wintypes.HANDLE(process_handle)):
                k32.CloseHandle(handle)
                return False
            self._handle = handle
            self._close = k32.CloseHandle
            return True
        except Exception as exc:  # 保険なので、失敗しても起動は続ける
            log.debug("ジョブオブジェクトを使えませんでした: %s", type(exc).__name__)
            return False

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            try:
                self._close(handle)
            except Exception:
                pass


def prepare_config(home: Path, *, isolated: bool = False, listen_address: str | None = None) -> None:
    """生成直後の config.xml を、起動前に FolderLink 向けに整える。

    いつも: 利用統計の送信を断る、自動アップグレード無効、クラッシュ報告無効、ブラウザーを開かない。
    isolated=True（テスト用）: 外部の探索・リレー・NAT 越えを全部切る。
    listen_address: 同期の待ち受けアドレス（例 "tcp://127.0.0.1:50123"）。None なら変更しない。
    """
    path = home / "config.xml"
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        raise HomeSetupError(f"エンジンの設定ファイルを読めませんでした（{type(exc).__name__}）。") from None
    root = tree.getroot()
    options = root.find("options")
    if options is None:
        options = ET.SubElement(root, "options")

    def set_option(name: str, value: str) -> None:
        for el in options.findall(name)[1:]:
            options.remove(el)
        el = options.find(name)
        if el is None:
            el = ET.SubElement(options, name)
        el.text = value

    set_option("urAccepted", "-1")
    set_option("autoUpgradeIntervalH", "0")
    set_option("crashReportingEnabled", "false")
    set_option("startBrowser", "false")
    if isolated:
        set_option("globalAnnounceEnabled", "false")
        set_option("localAnnounceEnabled", "false")
        set_option("relaysEnabled", "false")
        set_option("natEnabled", "false")
    if listen_address is not None:
        set_option("listenAddress", listen_address)
    tmp = path.with_name("config.xml.fl-tmp")
    tree.write(tmp, encoding="utf-8", xml_declaration=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 本体
class SyncthingManager:
    """1つの専用ホームに対する Syncthing の起動・確認・停止。"""

    def __init__(
        self,
        home: Path | str,
        api_key: str,
        *,
        binary: Path | str | None = None,
        log_path: Path | str | None = None,
        store: ConfigStore | None = None,
        isolated: bool = False,
        listen_address: str | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        runner: Callable[..., Any] = subprocess.run,
        client_factory: Callable[..., ApiClient] = ApiClient,
        port_picker: Callable[[], int] = find_free_port,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("API キーがありません。")
        register_secret(api_key)
        self.home = Path(home)
        self.binary = Path(binary) if binary is not None else default_binary_path()
        self.log_path = Path(log_path) if log_path is not None else self.home / "syncthing.log"
        self.isolated = isolated
        self.listen_address = listen_address
        self._api_key = api_key
        self._store = store
        self._popen = popen
        self._runner = runner
        self._client_factory = client_factory
        self._port_picker = port_picker
        self._clock = clock

        self._idle = threading.Event()  # 待ち専用（set しない）。time.sleep の代わり
        self._lock = _FileLock(self.home / LOCK_NAME)
        self._job = _KillOnCloseJob()
        self._proc: Any = None
        self._log_fh: Any = None
        self._client: ApiClient | None = None
        self._verified = False
        self._stopping = False
        self.port: int | None = None
        self.device_id: str | None = None
        self.version: str | None = None

    def __repr__(self) -> str:
        return f"SyncthingManager(home={str(self.home)!r}, port={self.port}, verified={self._verified})"

    # ---- 状態 ----
    @property
    def pid(self) -> int | None:
        return getattr(self._proc, "pid", None) if self._proc is not None else None

    @property
    def is_verified(self) -> bool:
        return self._verified

    def is_process_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def exit_code(self) -> int | None:
        return self._proc.poll() if self._proc is not None else None

    @property
    def client(self) -> ApiClient:
        """本人確認が済んだエンジンの API クライアント。済んでいなければ例外。"""
        if not self._verified or self._client is None:
            raise EngineNotVerifiedError("同期エンジンの確認が済んでいません。")
        if not self.is_process_alive():
            self._verified = False
            raise EngineExitedError("同期エンジンが停止しています。", self.exit_code())
        return self._client

    # ---- 専用ホーム ----
    def _run_tool(self, args: list[str], *, input_text: str | None = None, timeout: float = 60.0) -> str:
        if not self.binary.is_file():
            raise BinaryMissingError(
                "同期エンジン（syncthing.exe）が見つかりません。FolderLink を入れ直してください。"
            )
        try:
            result = self._runner(
                [str(self.binary), *args],
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=self._child_env(with_key=False),
                creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            raise HomeSetupError("同期エンジンの準備が時間内に終わりませんでした。") from None
        except OSError as exc:
            raise HomeSetupError(f"同期エンジンを実行できませんでした（{type(exc).__name__}）。") from None
        if result.returncode != 0:
            tail = redact((result.stderr or result.stdout or "").strip())[-300:]
            raise HomeSetupError(f"同期エンジンの準備に失敗しました（終了コード {result.returncode}）。{tail}")
        return result.stdout or ""

    def ensure_home(self) -> str:
        """専用ホームが無ければ作り、デバイス ID を返す。既にあれば鍵には触れない。"""
        self.home.mkdir(parents=True, exist_ok=True)
        fresh = not (self.home / "config.xml").is_file() or not (self.home / "cert.pem").is_file()
        if fresh:
            # GUI のパスワードもランダムにして、API キーを知らない他のプログラムが
            # ブラウザー経由で操作できないようにする（このパスワードは保存しない・使わない）。
            gui_password = secrets.token_urlsafe(24)
            register_secret(gui_password)
            self._run_tool(
                ["generate", f"--home={self.home}", "--gui-user=folderlink", "--gui-password=-"],
                input_text=gui_password + "\n",
            )
            prepare_config(self.home, isolated=self.isolated, listen_address=self.listen_address)
        elif self.isolated or self.listen_address is not None:
            prepare_config(self.home, isolated=self.isolated, listen_address=self.listen_address)
        out = self._run_tool(["device-id", f"--home={self.home}"])
        device_id = ""
        for line in out.splitlines():
            line = line.strip()
            if len(line) == 63 and line.count("-") == 7:
                device_id = line
        if not device_id:
            raise HomeSetupError("このPCのデバイス ID を取得できませんでした。")
        self.device_id = device_id
        return device_id

    # ---- 起動 ----
    def _child_env(self, *, with_key: bool) -> dict[str, str]:
        """子プロセスの環境変数。親から紛れ込む ST* を消し、必要なものだけ足す。"""
        # ST で始まる無関係な変数まで消さないよう、Syncthing が読むものだけを対象にする
        env = dict(os.environ)
        for name in list(env):
            upper = name.upper()
            if upper in _SYNCTHING_ENV or upper.startswith("STDEBUG") or upper.startswith("STLOG"):
                del env[name]
        env["STNOUPGRADE"] = "1"
        env["STVERSIONEXTRA"] = "FolderLink"
        if with_key:
            env["STGUIAPIKEY"] = self._api_key
            # 監視プロセスを挟まず、本体を直接動かす（PID が1つになり、確実に止められる）
            env["STMONITORED"] = "yes"
        return env

    def _serve_args(self, port: int) -> list[str]:
        # API キーはここに入れない（コマンドラインは他のプロセスから見える）
        return [
            str(self.binary),
            "serve",
            f"--home={self.home}",
            f"--gui-address=127.0.0.1:{port}",
            "--no-browser",
            "--no-console",
            "--no-restart",
            "--no-upgrade",
            "--log-file=-",
        ]

    def _cleanup_orphan(self) -> None:
        """前回の FolderLink が残したエンジンがあれば、本人確認のうえで正常終了させる。

        確認できない相手（別の Syncthing など）には、状態の問い合わせ以外なにもしない。
        PID を頼りに強制終了することもしない（PID は別のプロセスに再利用され得る）。
        """
        if self._store is None:
            return
        info = self._store.load_runtime()
        if not info.port or info.port in RESERVED_PORTS:
            self._store.clear_runtime()
            return
        client = self._client_factory(info.port, self._api_key, retries=0, timeout=(1.0, 3.0))
        try:
            try:
                their_id = client.my_id()
            except ApiError:
                return  # 応答なし、または別物。何もしない
            if their_id != self.device_id:
                log.info("記録にあったポートでは別のエンジンが応答しました。触れません。")
                return
            log.info("前回のエンジンが残っていたので、正常終了させます。")
            try:
                client.shutdown()
            except ApiError:
                return
            deadline = self._clock() + 15.0
            while self._clock() < deadline:
                try:
                    client.ping()
                except ApiError:
                    break
                self._idle.wait(0.2)
        finally:
            client.close()
            self._store.clear_runtime()

    def launch(self) -> int:
        """エンジンを起動して、すぐ戻る（起動完了は待たない）。選んだポートを返す。"""
        if self.is_process_alive():
            raise AlreadyRunningError("同期エンジンは既に起動しています。")
        if not self.binary.is_file():
            raise BinaryMissingError(
                "同期エンジン（syncthing.exe）が見つかりません。FolderLink を入れ直してください。"
            )
        self.home.mkdir(parents=True, exist_ok=True)
        if not self._lock.acquire():
            raise AlreadyRunningError("FolderLink は既に起動しています（同じ設定フォルダを使用中）。")
        try:
            if self.device_id is None:
                self.ensure_home()
            self._cleanup_orphan()
            port = self._port_picker()
            if port in RESERVED_PORTS:
                raise EngineError("内部エラー: 予約済みのポートが選ばれました。")
            self._verified = False
            self._stopping = False
            self._client = None
            self.port = port
            self._open_log()
            flags = (_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
            try:
                self._proc = self._popen(
                    self._serve_args(port),
                    stdin=subprocess.DEVNULL,
                    stdout=self._log_fh if self._log_fh is not None else subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    env=self._child_env(with_key=True),
                    cwd=str(self.home),
                    creationflags=flags,
                )
            except OSError as exc:
                raise EngineError(f"同期エンジンを起動できませんでした（{type(exc).__name__}）。") from None
            handle = getattr(self._proc, "_handle", None)
            if handle is not None:
                self._job.assign(int(handle))
            self._client = self._client_factory(port, self._api_key, retries=0, timeout=(1.0, 5.0))
            return port
        except BaseException:
            self._abort_launch()
            raise

    def _abort_launch(self) -> None:
        self._kill_own_process()
        self._close_log()
        self._lock.release()

    def _open_log(self) -> None:
        self._close_log()
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            if self.log_path.exists() and self.log_path.stat().st_size > LOG_ROTATE_BYTES:
                old = self.log_path.with_name(self.log_path.name + ".1")
                os.replace(self.log_path, old)
            self._log_fh = open(self.log_path, "ab")
        except OSError as exc:
            log.warning("エンジンのログを開けませんでした: %s", type(exc).__name__)
            self._log_fh = None

    def _close_log(self) -> None:
        fh, self._log_fh = self._log_fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def poll_ready(self) -> bool:
        """起動確認を1回だけ行う。準備できていれば True。まだなら False。

        自分のエンジンでないと分かった場合・プロセスが終了した場合は例外。
        """
        if self._verified:
            return True
        if self._proc is None or self._client is None:
            raise EngineError("同期エンジンはまだ起動されていません。")
        code = self._proc.poll()
        if code is not None:
            tail = self.log_tail(8)
            self._abort_launch()
            raise EngineExitedError(
                f"同期エンジンが起動直後に終了しました（終了コード {code}）。{tail}".strip(), code
            )
        try:
            their_id = self._client.my_id()
        except ApiAuthError:
            # 自分のプロセスは生きているのに、選んだポートの相手が自分の API キーを受け付けない
            # → そのポートにいるのは別のプログラム。自分のプロセスを止めて、相手には何もしない。
            self._abort_launch()
            raise ForeignEngineError(
                "選んだポートで別のプログラムが応答しました。同期エンジンではないため接続しません。"
            ) from None
        except ApiConnectionError:
            return False  # まだ待ち受けていない
        except ApiError:
            return False  # 起動途中の中途半端な応答
        if their_id != self.device_id:
            self._abort_launch()
            raise ForeignEngineError(
                "応答した同期エンジンのデバイス ID が、このPCのものと一致しません。接続を中止しました。"
            )
        # もう一度プロセスの生存を確かめてから「確認済み」にする
        if self._proc.poll() is not None:
            code = self._proc.poll()
            self._abort_launch()
            raise EngineExitedError(f"同期エンジンが終了しました（終了コード {code}）。", code)
        self._verified = True
        try:
            self.version = str(self._client.system_version().get("version") or "") or None
        except ApiError:
            self.version = None
        # 通常の操作用に、再試行つきのクライアントへ替える
        self._client.close()
        self._client = self._client_factory(self.port, self._api_key)
        if self._store is not None:
            self._store.save_runtime(RuntimeInfo(pid=self.pid, port=self.port, started_at=time.time(),
                                                 device_id=self.device_id))
        log.info("同期エンジンを確認しました（port=%s, version=%s）。", self.port, self.version)
        return True

    def start(self, cancel: threading.Event | None = None, timeout: float = 90.0,
              interval: float = 0.25) -> ApiClient:
        """起動して、確認が済むまで待つ。**画面のスレッドからは呼ばない**（ワーカースレッド用）。

        待ちは cancel（threading.Event）で即座に中断できる。time.sleep は使わない。
        """
        cancel = cancel or threading.Event()
        self.launch()
        deadline = self._clock() + timeout
        while True:
            if cancel.is_set():
                self.stop()
                raise StartCancelled("同期エンジンの起動を中断しました。")
            if self.poll_ready():
                return self.client
            if self._clock() >= deadline:
                tail = self.log_tail(8)
                self.stop()
                raise EngineStartTimeout(f"同期エンジンの起動を確認できませんでした。{tail}".strip())
            cancel.wait(interval)

    def verify(self) -> bool:
        """動作中に、相手がまだ自分のエンジンかを確かめ直す。違えば確認済みを取り消す。"""
        if not self._verified or self._client is None:
            return False
        if not self.is_process_alive():
            self._verified = False
            return False
        try:
            ok = self._client.my_id() == self.device_id
        except ApiError:
            return False  # 一時的に応答が無いだけかもしれないので、確認済みは取り消さない
        if not ok:
            self._verified = False
        return ok

    # ---- 停止 ----
    def _kill_own_process(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._job.close()  # ジョブに残っている子プロセスもここで終わる

    def stop(self, timeout: float = 15.0) -> None:
        """自分が起動したエンジンを止める。確認済みなら API で正常終了、だめなら強制終了。"""
        if self._stopping:
            return
        self._stopping = True
        try:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                if self._verified and self._client is not None:
                    try:
                        self._client.request("POST", "/rest/system/shutdown", expect_json=False,
                                             timeout=(1.0, 5.0))
                    except ApiError:
                        pass
                    try:
                        proc.wait(timeout=timeout)
                    except Exception:
                        pass
                self._kill_own_process()
            else:
                self._job.close()
        finally:
            self._verified = False
            if self._client is not None:
                self._client.close()
                self._client = None
            self._close_log()
            if self._store is not None:
                self._store.clear_runtime()
            self._lock.release()
            self._stopping = False

    # ---- ログ ----
    def log_tail(self, lines: int = 50) -> str:
        """エンジンのログの末尾（機密は消してある）。"""
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 64 * 1024))
                data = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        return redact("\n".join(data.splitlines()[-lines:]))


#: Syncthing が読む環境変数（親プロセスから紛れ込ませない）
_SYNCTHING_ENV = frozenset({
    "STHOMEDIR", "STCONFDIR", "STDATADIR", "STGUIADDRESS", "STGUIAPIKEY", "STNOBROWSER", "STHIDECONSOLE",
    "STNORESTART", "STNOUPGRADE", "STNOPORTPROBING", "STPAUSED", "STUNPAUSED", "STALLOWNEWERCONFIG",
    "STAUDIT", "STAUDITFILE", "STDBMAINTENANCEINTERVAL", "STDBDELETERETENTIONINTERVAL", "STTRACE",
    "STVERSIONEXTRA", "STMONITORED", "STGUIASSETS", "STPERFSTATS", "STBLOCKPROFILE", "STCPUPROFILE",
    "STHEAPPROFILE", "STPROFILER", "STNODEFAULTFOLDER", "STRECHECKDBEVERY", "STHASHING",
})
