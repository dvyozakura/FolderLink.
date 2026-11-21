"""テスト用: ApiClient の代わりになる、メモリ上の偽エンジン。

設定（デバイス・フォルダ）を覚えていて、状態の応答は属性を書き換えて自由に作れる。
`fail["メソッド名"] = 例外` で、その呼び出しだけ失敗させられる。
"""

from __future__ import annotations

import copy
from typing import Any

from folderlink.core.api_client import ApiNotFoundError

MY_ID = "MFZWI3D-BONSGYC-YLTMRWG-C43ENR5-QXGZDMM-FZWI3DP-BONSGYY-LTMRWAD"


class FakeClient:
    def __init__(self, my_id: str = MY_ID) -> None:
        self._my_id = my_id
        self.devices: dict[str, dict[str, Any]] = {my_id: {"deviceID": my_id, "name": "this-pc", "paused": False}}
        self.folders: dict[str, dict[str, Any]] = {}
        self.pending_dev: dict[str, Any] = {}
        self.pending_fol: dict[str, Any] = {}
        self.conns: dict[str, Any] = {"connections": {}, "total": {}}
        self.status: dict[str, dict[str, Any]] = {}
        self.completion: dict[tuple[str, str], dict[str, Any]] = {}
        self.folder_stats: dict[str, Any] = {}
        self.ignores: dict[str, list[str]] = {}
        self.versions: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self.need: dict[str, dict[str, Any]] = {}
        self.restore_errors: dict[str, str] = {}
        self.fail: dict[str, BaseException] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _enter(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if name in self.fail:
            raise self.fail[name]

    def called(self, name: str) -> list[tuple[Any, ...]]:
        return [a for n, a in self.calls if n == name]

    # ---- システム ----
    def my_id(self) -> str:
        self._enter("my_id")
        return self._my_id

    def ping(self) -> bool:
        self._enter("ping")
        return True

    def connections(self) -> dict[str, Any]:
        self._enter("connections")
        return copy.deepcopy(self.conns)

    def system_errors(self) -> list[dict[str, Any]]:
        self._enter("system_errors")
        return []

    def pause_device(self, device_id: str | None = None) -> None:
        self._enter("pause_device", device_id)
        for d in self.devices.values():
            if device_id in (None, d["deviceID"]):
                d["paused"] = True

    def resume_device(self, device_id: str | None = None) -> None:
        self._enter("resume_device", device_id)
        for d in self.devices.values():
            if device_id in (None, d["deviceID"]):
                d["paused"] = False

    # ---- 設定 ----
    def get_devices(self) -> list[dict[str, Any]]:
        self._enter("get_devices")
        return copy.deepcopy(list(self.devices.values()))

    def get_device(self, device_id: str) -> dict[str, Any]:
        self._enter("get_device", device_id)
        if device_id not in self.devices:
            raise ApiNotFoundError("対象が見つかりません。", status=404)
        return copy.deepcopy(self.devices[device_id])

    def default_device(self) -> dict[str, Any]:
        self._enter("default_device")
        return {"deviceID": "", "name": "", "addresses": ["dynamic"], "compression": "metadata",
                "introducer": False, "paused": False, "autoAcceptFolders": False, "maxSendKbps": 0}

    def add_device(self, device: dict[str, Any]) -> None:
        self._enter("add_device", copy.deepcopy(device))
        self.devices[device["deviceID"]] = copy.deepcopy(device)

    def patch_device(self, device_id: str, changes: dict[str, Any]) -> None:
        self._enter("patch_device", device_id, copy.deepcopy(changes))
        if device_id not in self.devices:
            raise ApiNotFoundError("対象が見つかりません。", status=404)
        self.devices[device_id].update(copy.deepcopy(changes))

    def delete_device(self, device_id: str) -> None:
        self._enter("delete_device", device_id)
        if device_id not in self.devices:
            raise ApiNotFoundError("対象が見つかりません。", status=404)
        del self.devices[device_id]

    def get_folders(self) -> list[dict[str, Any]]:
        self._enter("get_folders")
        return copy.deepcopy(list(self.folders.values()))

    def get_folder(self, folder_id: str) -> dict[str, Any]:
        self._enter("get_folder", folder_id)
        if folder_id not in self.folders:
            raise ApiNotFoundError("対象が見つかりません。", status=404)
        return copy.deepcopy(self.folders[folder_id])

    def default_folder(self) -> dict[str, Any]:
        self._enter("default_folder")
        return {"id": "", "label": "", "path": "~", "type": "sendreceive", "devices": [],
                "rescanIntervalS": 3600, "fsWatcherEnabled": True, "fsWatcherDelayS": 10,
                "ignorePerms": False, "paused": False, "maxConflicts": 10, "ignoreDelete": False,
                "markerName": ".stfolder", "minDiskFree": {"value": 1, "unit": "%"},
                "versioning": {"type": "", "params": {}, "cleanupIntervalS": 3600, "fsPath": "", "fsType": "basic"}}

    def add_folder(self, folder: dict[str, Any]) -> None:
        self._enter("add_folder", copy.deepcopy(folder))
        self.folders[folder["id"]] = copy.deepcopy(folder)

    def patch_folder(self, folder_id: str, changes: dict[str, Any]) -> None:
        self._enter("patch_folder", folder_id, copy.deepcopy(changes))
        if folder_id not in self.folders:
            raise ApiNotFoundError("対象が見つかりません。", status=404)
        self.folders[folder_id].update(copy.deepcopy(changes))

    def delete_folder(self, folder_id: str) -> None:
        self._enter("delete_folder", folder_id)
        if folder_id not in self.folders:
            raise ApiNotFoundError("対象が見つかりません。", status=404)
        del self.folders[folder_id]

    # ---- 状態 ----
    def db_status(self, folder_id: str) -> dict[str, Any]:
        self._enter("db_status", folder_id)
        return copy.deepcopy(self.status.get(folder_id, {}))

    def db_completion(self, folder_id: str | None = None, device_id: str | None = None) -> dict[str, Any]:
        self._enter("db_completion", folder_id, device_id)
        return copy.deepcopy(self.completion.get((folder_id or "", device_id or ""), {}))

    def db_need(self, folder_id: str, page: int = 1, perpage: int = 1000) -> dict[str, Any]:
        self._enter("db_need", folder_id, page, perpage)
        return copy.deepcopy(self.need.get(folder_id, {"progress": [], "queued": [], "rest": []}))

    def stats_folder(self) -> dict[str, Any]:
        self._enter("stats_folder")
        return copy.deepcopy(self.folder_stats)

    def folder_errors(self, folder_id: str) -> list[dict[str, Any]]:
        self._enter("folder_errors", folder_id)
        return []

    def scan(self, folder_id: str, sub: str | None = None) -> None:
        self._enter("scan", folder_id, sub)

    def events(self, since: int = 0, timeout_s: int = 30, events: list[str] | None = None,
               limit: int | None = None) -> list[dict[str, Any]]:
        self._enter("events", since, timeout_s, events, limit)
        return []

    # ---- ペアリング ----
    def pending_devices(self) -> dict[str, Any]:
        self._enter("pending_devices")
        return copy.deepcopy(self.pending_dev)

    def pending_folders(self) -> dict[str, Any]:
        self._enter("pending_folders")
        return copy.deepcopy(self.pending_fol)

    def dismiss_pending_device(self, device_id: str) -> None:
        self._enter("dismiss_pending_device", device_id)
        self.pending_dev.pop(device_id, None)

    def dismiss_pending_folder(self, folder_id: str, device_id: str | None = None) -> None:
        self._enter("dismiss_pending_folder", folder_id, device_id)
        self.pending_fol.pop(folder_id, None)

    # ---- 除外・履歴 ----
    def get_ignores(self, folder_id: str) -> list[str]:
        self._enter("get_ignores", folder_id)
        return list(self.ignores.get(folder_id, []))

    def set_ignores(self, folder_id: str, lines: list[str]) -> None:
        self._enter("set_ignores", folder_id, list(lines))
        self.ignores[folder_id] = list(lines)

    def folder_versions(self, folder_id: str) -> dict[str, list[dict[str, Any]]]:
        self._enter("folder_versions", folder_id)
        return copy.deepcopy(self.versions.get(folder_id, {}))

    def restore_versions(self, folder_id: str, choices: dict[str, str]) -> dict[str, str]:
        self._enter("restore_versions", folder_id, dict(choices))
        return dict(self.restore_errors)

    def close(self) -> None:
        pass
