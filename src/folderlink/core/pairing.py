"""ペアリング: デバイス ID の検証、相手の登録、承認待ちの取得と承認、フォルダの共有。

- デバイス ID は 56 文字（base32 の 52 文字 + 13 文字ごとのチェック文字 4 つ）。
  チェック文字の計算は Syncthing（lib/protocol/luhn.go）と同じ Luhn mod 32。
- 新しいデバイス・フォルダは、エンジンの `/rest/config/defaults/*` をひな形にして作る
  （必須フィールドの抜けを防ぐ）。
- 失敗は PairingError（利用者に見せてよい日本語の文面）にまとめる。
"""

from __future__ import annotations

import logging
import os
import secrets
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from folderlink.core.api_client import (
    ApiAuthError,
    ApiBadRequestError,
    ApiClient,
    ApiConnectionError,
    ApiError,
    ApiNotFoundError,
)

log = logging.getLogger(__name__)

_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_B32_SET = frozenset(_B32)

DEFAULT_VERSIONING_DAYS = 30
FOLDER_ID_PREFIX = "fl-"


# ---------------------------------------------------------------- 例外
class PairingError(Exception):
    """ペアリング・共有の失敗（基底）。"""


class DeviceIdError(PairingError):
    """デバイス ID の形式が正しくない。reason で種類が分かる。"""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason  # empty / chars / length / checksum / self


class PairingNetworkError(PairingError):
    """エンジンに繋がらず、操作できなかった（やり直せる）。"""


class PairingRejectedError(PairingError):
    """エンジンが要求を受け付けなかった。"""


class FolderPathError(PairingError):
    """同期フォルダとして使えない場所。"""


class FolderConflictError(PairingError):
    """同じフォルダ ID が、別の場所で既に使われている。"""


# ---------------------------------------------------------------- デバイス ID
def _luhn32(block: str) -> str:
    """13 文字のブロックに対するチェック文字（Syncthing と同じ計算）。"""
    factor = 1
    total = 0
    for ch in block:
        addend = factor * _B32.index(ch)
        factor = 1 if factor == 2 else 2
        addend = (addend // 32) + (addend % 32)
        total += addend
    return _B32[(32 - total % 32) % 32]


def _chunk(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


def normalize_device_id(text: str | None) -> str:
    """入力されたデバイス ID を検証し、正式な形（7文字×8組、ハイフン区切り）にして返す。

    - 前後や途中の空白・改行・ハイフンは無視する。小文字は大文字にする。
    - 見間違えやすい 0→O、1→I、8→B は読み替える（Syncthing と同じ）。
    - 56 文字ならチェック文字を検算する。古い 52 文字の形ならチェック文字を補う。
    """
    if text is None or not text.strip():
        raise DeviceIdError("デバイス ID を入力してください。", "empty")
    raw = "".join(text.split()).replace("-", "").upper()
    raw = raw.replace("0", "O").replace("1", "I").replace("8", "B")
    bad = sorted({c for c in raw if c not in _B32_SET})
    if bad:
        shown = " ".join(bad[:5])
        raise DeviceIdError(
            f"デバイス ID に使えない文字が含まれています（{shown}）。相手の画面からコピーして貼り付けてください。",
            "chars",
        )
    if len(raw) == 52:
        raw = "".join(block + _luhn32(block) for block in _chunk(raw, 13))
    elif len(raw) == 56:
        for block in _chunk(raw, 14):
            if _luhn32(block[:13]) != block[13]:
                raise DeviceIdError(
                    "デバイス ID が正しくありません（打ち間違いの可能性があります）。"
                    "相手の画面からコピーして貼り付けてください。",
                    "checksum",
                )
    else:
        raise DeviceIdError(
            f"デバイス ID の長さが正しくありません（英数字 56 文字が必要です。入力は {len(raw)} 文字）。",
            "length",
        )
    return "-".join(_chunk(raw, 7))


def is_valid_device_id(text: str | None) -> bool:
    try:
        normalize_device_id(text)
        return True
    except DeviceIdError:
        return False


def short_device_id(device_id: str) -> str:
    """画面用の短い表記（先頭の 7 文字）。"""
    return (device_id or "").split("-")[0]


# ---------------------------------------------------------------- フォルダ
def generate_folder_id() -> str:
    """2台で共通に使うフォルダ ID（例: fl-k3m9x-q2w8e）。"""
    alphabet = string.ascii_lowercase + string.digits
    part = lambda: "".join(secrets.choice(alphabet) for _ in range(5))  # noqa: E731
    return f"{FOLDER_ID_PREFIX}{part()}-{part()}"


def derive_folder_id(device_a: str, device_b: str) -> str:
    """2台のデバイス ID から決まるフォルダ ID。

    どちらの PC で計算しても同じ値になるので、両方の PC が同時に「同期開始」を押しても
    フォルダ ID が食い違わない（片方の申し出を待つ必要がない）。
    """
    import base64
    import hashlib

    a, b = sorted((normalize_device_id(device_a), normalize_device_id(device_b)))
    if a == b:
        raise DeviceIdError("2台のデバイス ID が同じです。", "self")
    digest = hashlib.sha256(f"FolderLink|{a}|{b}".encode("ascii")).digest()
    text = base64.b32encode(digest).decode("ascii").lower()
    return f"{FOLDER_ID_PREFIX}{text[:5]}-{text[5:10]}"


def is_valid_folder_id(folder_id: str | None) -> bool:
    if not folder_id or len(folder_id) > 64:
        return False
    return all(c.isascii() and (c.isalnum() or c in "-_.") for c in folder_id)


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path))).rstrip("\\/")


def _is_same_or_inside(child: Path, parent: Path) -> bool:
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p + os.sep)


def validate_sync_folder(path: str | Path, *, forbidden: list[Path] | None = None,
                         must_exist: bool = True) -> Path:
    """同期フォルダとして使ってよい場所かを確かめる。だめなら FolderPathError。

    断る場所: 相対パス、ドライブの直下、ユーザーフォルダそのもの、Windows / Program Files の中、
    FolderLink 自身の設定フォルダと関係する場所（forbidden で渡す）。
    """
    if path is None or not str(path).strip():
        raise FolderPathError("同期するフォルダを選んでください。")
    p = Path(str(path).strip())
    if not p.is_absolute():
        raise FolderPathError("フォルダは絶対パス（C:\\ から始まる場所）で指定してください。")
    p = Path(os.path.abspath(str(p)))
    if p.parent == p:
        raise FolderPathError("ドライブ全体は同期できません。ドライブの中のフォルダを選んでください。")
    if must_exist and not p.is_dir():
        raise FolderPathError("そのフォルダが見つかりません。")
    if p.exists() and not p.is_dir():
        raise FolderPathError("フォルダではなくファイルが指定されています。")
    home = Path.home()
    if _norm(p) == _norm(home):
        raise FolderPathError("ユーザーフォルダ全体は同期できません。その中のフォルダを選んでください。")
    for env in ("WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        base = os.environ.get(env)
        if base and _is_same_or_inside(p, Path(base)):
            raise FolderPathError("Windows やアプリのフォルダは同期できません。")
    for bad in forbidden or []:
        if _is_same_or_inside(p, bad) or _is_same_or_inside(bad, p):
            raise FolderPathError("FolderLink 自身の設定が入っているフォルダは同期できません。")
    return p


@dataclass(frozen=True)
class FolderContents:
    """初回同期前プレビュー用: フォルダに今あるものの集計。"""

    files: int
    dirs: int
    bytes: int
    truncated: bool  # 件数が多すぎて途中で数えるのをやめた

    @property
    def is_empty(self) -> bool:
        return self.files == 0 and self.dirs == 0


_SKIP_NAMES = frozenset({".stfolder", ".stversions", ".stignore"})


def scan_folder_contents(path: str | Path, limit: int = 200_000) -> FolderContents:
    """フォルダの中のファイル数・容量を数える（Syncthing の管理用ファイルは除く）。"""
    files = dirs = size = 0
    truncated = False
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.name in _SKIP_NAMES or entry.name.startswith("~syncthing~"):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    dirs += 1
                    stack.append(entry.path)
                else:
                    files += 1
                    size += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            if files + dirs >= limit:
                truncated = True
                stack.clear()
                break
    return FolderContents(files=files, dirs=dirs, bytes=size, truncated=truncated)


# ---------------------------------------------------------------- データ
@dataclass(frozen=True)
class PendingDevice:
    device_id: str
    name: str
    address: str
    time: str


@dataclass(frozen=True)
class PendingFolder:
    folder_id: str
    label: str
    device_id: str
    time: str


@dataclass(frozen=True)
class PeerState:
    configured: bool
    connected: bool
    paused: bool
    address: str | None = None
    via_relay: bool | None = None
    client_version: str | None = None


# ---------------------------------------------------------------- 本体
class Pairing:
    def __init__(self, client: ApiClient, my_id: str) -> None:
        self.client = client
        self.my_id = normalize_device_id(my_id)

    # ---- 共通: API の失敗を利用者向けの例外へ ----
    @staticmethod
    def _wrap(exc: ApiError, doing: str) -> PairingError:
        if isinstance(exc, ApiAuthError):
            return PairingRejectedError(f"{doing}できませんでした。同期エンジンに認証を拒否されました。")
        if isinstance(exc, ApiConnectionError):
            return PairingNetworkError(
                f"{doing}できませんでした。同期エンジンに接続できません。少し待ってからやり直してください。"
            )
        if isinstance(exc, (ApiBadRequestError, ApiNotFoundError)):
            return PairingRejectedError(f"{doing}できませんでした。同期エンジンが要求を受け付けませんでした（{exc}）。")
        return PairingError(f"{doing}できませんでした（{exc}）。")

    # ---- デバイス ----
    def check_peer_id(self, text: str | None) -> str:
        """相手のデバイス ID を検証する。自分自身の ID は断る。"""
        device_id = normalize_device_id(text)
        if device_id == self.my_id:
            raise DeviceIdError(
                "これはこのPC自身のデバイス ID です。相手のPCに表示されている ID を入力してください。", "self"
            )
        return device_id

    def add_device(self, device_id: str, name: str = "", addresses: list[str] | None = None) -> str:
        """相手を登録する。既に登録済みなら名前・アドレスだけ更新する。正式な形の ID を返す。"""
        device_id = self.check_peer_id(device_id)
        name = (name or "").strip()[:64]
        try:
            existing = {d.get("deviceID"): d for d in self.client.get_devices()}
            if device_id in existing:
                changes: dict[str, Any] = {"paused": False}
                if name:
                    changes["name"] = name
                if addresses:
                    changes["addresses"] = list(addresses)
                self.client.patch_device(device_id, changes)
                return device_id
            device = dict(self.client.default_device())
            device.update({
                "deviceID": device_id,
                "name": name or short_device_id(device_id),
                "addresses": list(addresses) if addresses else ["dynamic"],
                "introducer": False,
                "autoAcceptFolders": False,
                "paused": False,
            })
            self.client.add_device(device)
        except ApiError as exc:
            raise self._wrap(exc, "相手のPCを登録") from None
        return device_id

    def remove_device(self, device_id: str) -> None:
        device_id = self.check_peer_id(device_id)
        try:
            self.client.delete_device(device_id)
        except ApiNotFoundError:
            return
        except ApiError as exc:
            raise self._wrap(exc, "相手のPCの登録を解除") from None

    def configured_peers(self) -> list[dict[str, Any]]:
        """登録済みの相手（自分自身を除く）。"""
        try:
            return [d for d in self.client.get_devices() if d.get("deviceID") != self.my_id]
        except ApiError as exc:
            raise self._wrap(exc, "登録済みのPCを取得") from None

    def pending_devices(self) -> list[PendingDevice]:
        """こちらに接続を求めてきているが、まだ登録していないデバイス。"""
        try:
            raw = self.client.pending_devices()
        except ApiError as exc:
            raise self._wrap(exc, "承認待ちのPCを取得") from None
        result = []
        for device_id, info in raw.items():
            info = info if isinstance(info, dict) else {}
            result.append(PendingDevice(device_id, str(info.get("name") or ""), str(info.get("address") or ""),
                                        str(info.get("time") or "")))
        return sorted(result, key=lambda d: d.time, reverse=True)

    def accept_pending_device(self, device_id: str, name: str = "") -> str:
        return self.add_device(device_id, name)

    def reject_pending_device(self, device_id: str) -> None:
        try:
            self.client.dismiss_pending_device(device_id)
        except ApiNotFoundError:
            return
        except ApiError as exc:
            raise self._wrap(exc, "承認待ちを取り消し") from None

    def peer_state(self, device_id: str) -> PeerState:
        """相手との接続の様子。未登録・未接続でも例外にしない。"""
        try:
            configured = any(d.get("deviceID") == device_id for d in self.client.get_devices())
            conns = self.client.connections().get("connections") or {}
        except ApiError as exc:
            raise self._wrap(exc, "接続状態を取得") from None
        info = conns.get(device_id) if isinstance(conns, dict) else None
        if not isinstance(info, dict):
            return PeerState(configured=configured, connected=False, paused=False)
        conn_type = str(info.get("type") or "")
        connected = bool(info.get("connected"))
        return PeerState(
            configured=configured,
            connected=connected,
            paused=bool(info.get("paused")),
            address=(str(info.get("address")) or None) if connected else None,
            via_relay=("relay" in conn_type) if connected and conn_type else None,
            client_version=(str(info.get("clientVersion")) or None) if connected else None,
        )

    # ---- フォルダ ----
    def pending_folders(self) -> list[PendingFolder]:
        """相手が共有を申し出ているが、こちらにまだ無いフォルダ。"""
        try:
            raw = self.client.pending_folders()
        except ApiError as exc:
            raise self._wrap(exc, "共有の申し出を取得") from None
        result = []
        for folder_id, info in raw.items():
            offered = (info or {}).get("offeredBy") if isinstance(info, dict) else None
            for device_id, detail in (offered or {}).items():
                detail = detail if isinstance(detail, dict) else {}
                result.append(PendingFolder(folder_id, str(detail.get("label") or ""), device_id,
                                            str(detail.get("time") or "")))
        return sorted(result, key=lambda f: f.time, reverse=True)

    def reject_pending_folder(self, folder_id: str, device_id: str | None = None) -> None:
        try:
            self.client.dismiss_pending_folder(folder_id, device_id)
        except ApiNotFoundError:
            return
        except ApiError as exc:
            raise self._wrap(exc, "共有の申し出を取り消し") from None

    def share_folder(
        self,
        folder_id: str,
        path: str | Path,
        peer_id: str,
        *,
        label: str = "FolderLink",
        versioning_days: int = DEFAULT_VERSIONING_DAYS,
        paused: bool = False,
        forbidden: list[Path] | None = None,
        folder_type: str = "sendreceive",
    ) -> dict[str, Any]:
        """フォルダを作り（無ければ）、相手と共有する。作った（または更新した）設定を返す。

        - folder_id は2台で共通。path は PC ごとに違ってよい。
        - 同じ folder_id が**別の場所**で既に使われていたら断る（場所を黙って付け替えると、
          元の場所のファイルが「消えた」と扱われる危険があるため）。
        - 過去版を残す設定（staggered）を必ず付ける。競合コピーを作らない設定（maxConflicts=0）にはしない。
        - folder_type="sendonly" は初回同期前プレビュー用。相手と一覧（インデックス）だけを交換し、
          このPCのファイルは一切書き換えない。"sendreceive" で通常の双方向同期になる。
        """
        if not is_valid_folder_id(folder_id):
            raise PairingError("フォルダ ID が正しくありません。")
        if folder_type not in ("sendreceive", "sendonly"):
            raise PairingError("フォルダの種類が正しくありません。")
        peer_id = self.check_peer_id(peer_id)
        folder_path = validate_sync_folder(path, forbidden=forbidden)
        days = versioning_days if isinstance(versioning_days, int) and 1 <= versioning_days <= 3650 \
            else DEFAULT_VERSIONING_DAYS
        versioning = staggered_versioning(days)
        try:
            folders = {f.get("id"): f for f in self.client.get_folders()}
            for other in folders.values():
                if other.get("id") != folder_id and other.get("path") and \
                        _norm(Path(str(other["path"]))) == _norm(folder_path):
                    raise FolderConflictError("このフォルダは、既に別の同期設定で使われています。")
            if folder_id in folders:
                current = folders[folder_id]
                if _norm(Path(str(current.get("path") or ""))) != _norm(folder_path):
                    raise FolderConflictError(
                        "この同期設定は、既に別のフォルダ（" + str(current.get("path")) + "）で使われています。"
                        "フォルダを変える場合は、いったん同期設定を解除してから設定し直してください。"
                    )
                device_ids = [d.get("deviceID") for d in current.get("devices") or []]
                devices = list(current.get("devices") or [])
                for wanted in (self.my_id, peer_id):
                    if wanted not in device_ids:
                        devices.append({"deviceID": wanted})
                changes: dict[str, Any] = {"devices": devices, "paused": paused, "type": folder_type}
                if (current.get("versioning") or {}).get("type") in ("", None):
                    changes["versioning"] = versioning
                if current.get("maxConflicts") == 0:
                    changes["maxConflicts"] = 10
                self.client.patch_folder(folder_id, changes)
                return self.client.get_folder(folder_id)
            folder = dict(self.client.default_folder())
            base_versioning = dict(folder.get("versioning") or {})
            base_versioning.update(versioning)
            folder.update({
                "id": folder_id,
                "label": (label or "FolderLink").strip()[:64],
                "path": str(folder_path),
                "type": folder_type,
                "devices": [{"deviceID": self.my_id}, {"deviceID": peer_id}],
                "paused": paused,
                "ignoreDelete": False,
                "versioning": base_versioning,
            })
            if folder.get("maxConflicts") == 0:
                folder["maxConflicts"] = 10
            self.client.add_folder(folder)
            return self.client.get_folder(folder_id)
        except PairingError:
            raise
        except ApiError as exc:
            raise self._wrap(exc, "フォルダを共有") from None

    def unshare_folder(self, folder_id: str) -> None:
        """同期設定を解除する。**フォルダの中のファイルには触れない。**"""
        try:
            self.client.delete_folder(folder_id)
        except ApiNotFoundError:
            return
        except ApiError as exc:
            raise self._wrap(exc, "同期設定を解除") from None

    def set_folder_paused(self, folder_id: str, paused: bool) -> None:
        try:
            self.client.patch_folder(folder_id, {"paused": bool(paused)})
        except ApiError as exc:
            raise self._wrap(exc, "一時停止を切り替え") from None


def staggered_versioning(days: int) -> dict[str, Any]:
    """staggered バージョニングの設定（maxAge は秒）。"""
    return {
        "type": "staggered",
        "params": {"maxAge": str(int(days) * 86400)},
        "cleanupIntervalS": 3600,
    }
