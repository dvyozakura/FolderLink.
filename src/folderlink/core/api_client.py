"""Syncthing のローカル REST API クライアント。

- 接続先は 127.0.0.1 だけ（それ以外は作る時点で拒否）
- 認証は X-API-Key ヘッダー
- タイムアウトと再試行（やり直しても安全な呼び出しだけ）
- エラーを種類分けして例外にする
- API キーを例外メッセージ・repr・ログに出さない

エンドポイントの出どころは Syncthing の公式文書 https://docs.syncthing.net/dev/rest.html（v2.1.7 で確認）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable
from urllib.parse import quote

import requests

from folderlink.core.redact import redact, register_secret

log = logging.getLogger(__name__)

ALLOWED_HOST = "127.0.0.1"
DEFAULT_TIMEOUT = (3.0, 15.0)  # (接続, 読み取り) 秒


# ---------------------------------------------------------------- 例外
class ApiError(Exception):
    """API 呼び出しの失敗（基底）。message は利用者に見せてよい日本語。"""

    def __init__(self, message: str, *, status: int | None = None, detail: str = "") -> None:
        super().__init__(redact(message))
        self.status = status
        self.detail = redact(detail)

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base}（{self.detail}）" if self.detail else base


class ApiConnectionError(ApiError):
    """エンジンに繋がらない（起動していない・落ちた・ポート違い）。"""


class ApiTimeoutError(ApiConnectionError):
    """時間内に応答が無い。"""


class ApiAuthError(ApiError):
    """API キーが違う（401/403）。別の Syncthing に繋いでいる可能性がある。"""


class ApiNotFoundError(ApiError):
    """対象が無い（404）。フォルダ・デバイスが未登録など。"""


class ApiFolderPausedError(ApiError):
    """フォルダが一時停止中で、状態を取得できない。"""


class ApiBadRequestError(ApiError):
    """送った内容が不正（400 など）。"""


class ApiServerError(ApiError):
    """エンジン側のエラー（5xx）。"""


class ApiResponseError(ApiError):
    """応答が想定した形でない（JSON でない、Syncthing ではない など）。"""


# ---------------------------------------------------------------- クライアント
_RETRY_METHODS = {"GET", "PUT", "DELETE"}  # 同じ要求をもう一度送っても結果が変わらないもの
_PAUSED_HINTS = ("folder is paused", "folder paused", "is paused")


class ApiClient:
    def __init__(
        self,
        port: int,
        api_key: str,
        *,
        host: str = ALLOWED_HOST,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
        retries: int = 2,
        backoff: float = 0.3,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if host != ALLOWED_HOST:
            raise ValueError("FolderLink は 127.0.0.1 のエンジンにしか接続しません。")
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            raise ValueError("ポート番号が正しくありません。")
        if not api_key:
            raise ValueError("API キーがありません。")
        register_secret(api_key)
        self.host = host
        self.port = port
        self._api_key = api_key
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.backoff = backoff
        self._sleep = sleep
        if session is None:
            session = requests.Session()
            # プロキシの環境変数や .netrc を使わない（ローカル通信を外へ回さない）
            session.trust_env = False
        self._session = session

    def __repr__(self) -> str:
        return f"ApiClient(http://{self.host}:{self.port}, api_key=***)"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    # ---- 中心 ----
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout: tuple[float, float] | None = None,
        retries: int | None = None,
        expect_json: bool = True,
    ) -> Any:
        method = method.upper()
        if not path.startswith("/rest/"):
            raise ValueError("REST のパスは /rest/ で始めてください。")
        url = self.base_url + path
        what = f"{method} {path}"
        attempts = 1 + (self.retries if retries is None else max(0, retries))
        if method not in _RETRY_METHODS:
            attempts = 1
        last_error: ApiError | None = None
        for attempt in range(attempts):
            if attempt:
                self._sleep(self.backoff * (2 ** (attempt - 1)))
            try:
                resp = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={"X-API-Key": self._api_key},
                    timeout=timeout or self.timeout,
                    allow_redirects=False,
                )
            except requests.exceptions.Timeout:
                last_error = ApiTimeoutError("同期エンジンからの応答がありません。", detail=what)
                continue
            except requests.exceptions.ConnectionError:
                last_error = ApiConnectionError("同期エンジンに接続できません。", detail=what)
                continue
            except requests.exceptions.RequestException as exc:
                # 例外の文面には URL などが入るので、種類だけ残す
                last_error = ApiConnectionError(
                    "同期エンジンとの通信に失敗しました。", detail=f"{what}: {type(exc).__name__}"
                )
                continue
            try:
                return self._handle(resp, what, expect_json)
            except ApiServerError as exc:
                last_error = exc
                if resp.status_code in (502, 503, 504):
                    continue  # 一時的な失敗はやり直す
                raise
        assert last_error is not None
        raise last_error

    def _handle(self, resp: Any, what: str, expect_json: bool) -> Any:
        status = resp.status_code
        if 200 <= status < 300:
            if not expect_json:
                return resp.text
            text = resp.text or ""
            if not text.strip():
                return None
            try:
                return resp.json()
            except ValueError:
                raise ApiResponseError(
                    "同期エンジンの応答を読み取れませんでした。", status=status, detail=what
                ) from None
        body = redact((resp.text or "").strip())[:300]
        detail = f"{what}: HTTP {status} {body}".strip()
        lowered = body.lower()
        if status in (401, 403):
            raise ApiAuthError(
                "同期エンジンに認証を拒否されました（API キーが一致しません）。", status=status, detail=what
            )
        if any(h in lowered for h in _PAUSED_HINTS):
            raise ApiFolderPausedError("フォルダは一時停止中です。", status=status, detail=detail)
        if status == 404:
            raise ApiNotFoundError("対象が見つかりません。", status=status, detail=detail)
        if 400 <= status < 500:
            raise ApiBadRequestError("同期エンジンが要求を受け付けませんでした。", status=status, detail=detail)
        if 300 <= status < 400:
            raise ApiResponseError("同期エンジンから想定外の応答がありました。", status=status, detail=what)
        raise ApiServerError("同期エンジンでエラーが起きました。", status=status, detail=detail)

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=_clean(params))

    # ---- システム ----
    def ping(self) -> bool:
        data = self.request("GET", "/rest/system/ping", retries=0)
        return isinstance(data, dict) and data.get("ping") == "pong"

    def system_status(self) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/system/status"), "system/status")

    def my_id(self) -> str:
        my_id = self.system_status().get("myID")
        if not isinstance(my_id, str) or not my_id:
            raise ApiResponseError("同期エンジンのデバイス ID を取得できませんでした。")
        return my_id

    def system_version(self) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/system/version"), "system/version")

    def connections(self) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/system/connections"), "system/connections")

    def system_errors(self) -> list[dict[str, Any]]:
        data = self.get("/rest/system/error")
        errors = data.get("errors") if isinstance(data, dict) else None
        return list(errors or [])

    def shutdown(self) -> None:
        self.request("POST", "/rest/system/shutdown", expect_json=False)

    def pause_device(self, device_id: str | None = None) -> None:
        self.request("POST", "/rest/system/pause", params=_clean({"device": device_id}), expect_json=False)

    def resume_device(self, device_id: str | None = None) -> None:
        self.request("POST", "/rest/system/resume", params=_clean({"device": device_id}), expect_json=False)

    # ---- 設定 ----
    def get_devices(self) -> list[dict[str, Any]]:
        return _expect_list(self.get("/rest/config/devices"), "config/devices")

    def get_device(self, device_id: str) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/config/devices/" + quote(device_id, safe="")), "config/devices/id")

    def default_device(self) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/config/defaults/device"), "config/defaults/device")

    def add_device(self, device: dict[str, Any]) -> None:
        self.request("POST", "/rest/config/devices", json_body=device, expect_json=False)

    def patch_device(self, device_id: str, changes: dict[str, Any]) -> None:
        self.request("PATCH", "/rest/config/devices/" + quote(device_id, safe=""), json_body=changes,
                     expect_json=False)

    def delete_device(self, device_id: str) -> None:
        self.request("DELETE", "/rest/config/devices/" + quote(device_id, safe=""), expect_json=False)

    def get_folders(self) -> list[dict[str, Any]]:
        return _expect_list(self.get("/rest/config/folders"), "config/folders")

    def get_folder(self, folder_id: str) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/config/folders/" + quote(folder_id, safe="")), "config/folders/id")

    def default_folder(self) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/config/defaults/folder"), "config/defaults/folder")

    def add_folder(self, folder: dict[str, Any]) -> None:
        self.request("POST", "/rest/config/folders", json_body=folder, expect_json=False)

    def patch_folder(self, folder_id: str, changes: dict[str, Any]) -> None:
        self.request("PATCH", "/rest/config/folders/" + quote(folder_id, safe=""), json_body=changes,
                     expect_json=False)

    def delete_folder(self, folder_id: str) -> None:
        self.request("DELETE", "/rest/config/folders/" + quote(folder_id, safe=""), expect_json=False)

    def get_options(self) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/config/options"), "config/options")

    def patch_options(self, changes: dict[str, Any]) -> None:
        self.request("PATCH", "/rest/config/options", json_body=changes, expect_json=False)

    def restart_required(self) -> bool:
        data = self.get("/rest/config/restart-required")
        return bool(isinstance(data, dict) and data.get("requiresRestart"))

    # ---- 状態 ----
    def db_status(self, folder_id: str) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/db/status", folder=folder_id), "db/status")

    def db_completion(self, folder_id: str | None = None, device_id: str | None = None) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/db/completion", folder=folder_id, device=device_id), "db/completion")

    def db_need(self, folder_id: str, page: int = 1, perpage: int = 1000) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/db/need", folder=folder_id, page=page, perpage=perpage), "db/need")

    def folder_errors(self, folder_id: str) -> list[dict[str, Any]]:
        data = self.get("/rest/folder/errors", folder=folder_id)
        errors = data.get("errors") if isinstance(data, dict) else None
        return list(errors or [])

    def stats_folder(self) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/stats/folder"), "stats/folder")

    def stats_device(self) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/stats/device"), "stats/device")

    def scan(self, folder_id: str, sub: str | None = None) -> None:
        self.request("POST", "/rest/db/scan", params=_clean({"folder": folder_id, "sub": sub}), expect_json=False)

    def revert(self, folder_id: str) -> None:
        self.request("POST", "/rest/db/revert", params={"folder": folder_id}, expect_json=False)

    def events(self, since: int = 0, timeout_s: int = 30, events: list[str] | None = None,
               limit: int | None = None) -> list[dict[str, Any]]:
        """イベントを待つ（最長 timeout_s 秒）。何も無ければ空のリスト。"""
        params = _clean({
            "since": since,
            "timeout": timeout_s,
            "events": ",".join(events) if events else None,
            "limit": limit,
        })
        data = self.request("GET", "/rest/events", params=params,
                            timeout=(self.timeout[0], timeout_s + 10.0), retries=0)
        return list(data or [])

    # ---- ペアリング ----
    def pending_devices(self) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/cluster/pending/devices") or {}, "cluster/pending/devices")

    def pending_folders(self) -> dict[str, Any]:
        return _expect_dict(self.get("/rest/cluster/pending/folders") or {}, "cluster/pending/folders")

    def dismiss_pending_device(self, device_id: str) -> None:
        self.request("DELETE", "/rest/cluster/pending/devices", params={"device": device_id}, expect_json=False)

    def dismiss_pending_folder(self, folder_id: str, device_id: str | None = None) -> None:
        self.request("DELETE", "/rest/cluster/pending/folders",
                     params=_clean({"folder": folder_id, "device": device_id}), expect_json=False)

    # ---- 除外・履歴 ----
    def get_ignores(self, folder_id: str) -> list[str]:
        data = self.get("/rest/db/ignores", folder=folder_id)
        lines = data.get("ignore") if isinstance(data, dict) else None
        return list(lines or [])

    def set_ignores(self, folder_id: str, lines: list[str]) -> None:
        self.request("POST", "/rest/db/ignores", params={"folder": folder_id}, json_body={"ignore": lines},
                     expect_json=False)

    def folder_versions(self, folder_id: str) -> dict[str, list[dict[str, Any]]]:
        return _expect_dict(self.get("/rest/folder/versions", folder=folder_id) or {}, "folder/versions")

    def restore_versions(self, folder_id: str, choices: dict[str, str]) -> dict[str, str]:
        """過去版を復元する。choices は {パス: versionTime}。返り値は失敗したものの {パス: 理由}。"""
        data = self.request("POST", "/rest/folder/versions", params={"folder": folder_id}, json_body=choices)
        return dict(data or {})


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def _expect_dict(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ApiResponseError("同期エンジンから想定外の応答がありました。", detail=what)
    return data


def _expect_list(data: Any, what: str) -> list[Any]:
    if not isinstance(data, list):
        raise ApiResponseError("同期エンジンから想定外の応答がありました。", detail=what)
    return data
