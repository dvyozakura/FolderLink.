"""設定の保存と読み込み。

保存先（製品版）: %LOCALAPPDATA%\\FolderLink\\
    settings.json   画面・同期の設定（機密は入れない）
    secrets.json    機密（Syncthing の API キー）。settings.json とは別ファイル
    runtime.json    いま動いているエンジンの PID・ポート（次回起動時の照合用）
    syncthing\\      Syncthing 専用のホーム（設定・鍵・データベース）
    logs\\           ログ

開発・テストでは環境変数 FOLDERLINK_DATA_DIR で保存先を差し替える（安全ルール:
テストは _sandbox/ か一時フォルダだけを使い、%LOCALAPPDATA%\\FolderLink の実物に触れない）。

壊れたファイルへの対処:
    読めない設定ファイルは「<名前>.broken-<日時>」に改名して残し、
    直前の正常な控え（<名前>.bak）があればそれを使い、無ければ既定値で続行する。
"""

from __future__ import annotations

import json
import logging
import os
import secrets as _secrets
import shutil
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from folderlink.core.redact import register_secret

log = logging.getLogger(__name__)

ENV_DATA_DIR = "FOLDERLINK_DATA_DIR"
APP_DIR_NAME = "FolderLink"
SCHEMA_VERSION = 1

THEMES = ("system", "light", "dark")


def data_dir() -> Path:
    """設定の保存先フォルダ（作成はしない）。"""
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_DIR_NAME
    return Path.home() / "AppData" / "Local" / APP_DIR_NAME


@dataclass
class Settings:
    """画面と同期の設定。機密は入れない。"""

    schema_version: int = SCHEMA_VERSION
    theme: str = "system"
    setup_completed: bool = False
    folder_path: str | None = None
    folder_id: str | None = None
    folder_label: str = "FolderLink"
    peer_device_id: str | None = None
    peer_name: str = ""
    ignore_presets: list[str] = field(default_factory=list)
    custom_ignores: list[str] = field(default_factory=list)
    versioning_days: int = 30
    start_minimized: bool = False
    mass_delete_threshold: int = 50
    mass_delete_guard_enabled: bool = True

    def validate(self) -> None:
        """おかしな値を既定値に戻す（例外は出さない）。"""
        defaults = Settings()
        if self.theme not in THEMES:
            self.theme = defaults.theme
        if not isinstance(self.versioning_days, int) or isinstance(self.versioning_days, bool) \
                or not (1 <= self.versioning_days <= 3650):
            self.versioning_days = defaults.versioning_days
        if not isinstance(self.mass_delete_threshold, int) or isinstance(self.mass_delete_threshold, bool) \
                or self.mass_delete_threshold < 1:
            self.mass_delete_threshold = defaults.mass_delete_threshold
        self.ignore_presets = [s for s in self.ignore_presets if isinstance(s, str)]
        self.custom_ignores = [s for s in self.custom_ignores if isinstance(s, str)]
        self.schema_version = SCHEMA_VERSION


@dataclass
class Secrets:
    """機密。ログ・画面・クラッシュ報告に出してはいけない。"""

    api_key: str = ""

    def __repr__(self) -> str:  # うっかり print / ログしても中身が出ないように
        return "Secrets(api_key=***)"

    __str__ = __repr__


@dataclass
class RuntimeInfo:
    """動いているエンジンの控え（機密ではない）。"""

    pid: int | None = None
    port: int | None = None
    started_at: float | None = None
    device_id: str | None = None


def _type_ok(value: Any, default: Any, annotation: str) -> bool:
    if value is None:
        return "None" in annotation
    if isinstance(default, bool) or annotation == "bool":
        return isinstance(value, bool)
    if isinstance(default, int) or annotation.startswith("int"):
        return isinstance(value, int) and not isinstance(value, bool)
    if annotation.startswith("float"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, list) or annotation.startswith("list"):
        return isinstance(value, list)
    if isinstance(default, str) or annotation.startswith("str"):
        return isinstance(value, str)
    return True


def _from_dict(cls, raw: dict[str, Any]):
    """知らないキーは捨て、型が違う値は既定値にする。"""
    obj = cls()
    for f in fields(cls):
        if f.name not in raw:
            continue
        value = raw[f.name]
        if _type_ok(value, getattr(obj, f.name), str(f.type)):
            setattr(obj, f.name, value)
        else:
            log.warning("設定 %s の値の型が正しくないため、既定値を使います。", f.name)
    return obj


class ConfigStore:
    """設定・機密・実行時情報の読み書き。"""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else data_dir()
        #: 直近の load で壊れたファイルを見つけたときの説明（画面で知らせる用）。無ければ空。
        self.recovery_notes: list[str] = []

    # ---- 場所 ----
    @property
    def settings_path(self) -> Path:
        return self.base_dir / "settings.json"

    @property
    def secrets_path(self) -> Path:
        return self.base_dir / "secrets.json"

    @property
    def runtime_path(self) -> Path:
        return self.base_dir / "runtime.json"

    @property
    def syncthing_home(self) -> Path:
        return self.base_dir / "syncthing"

    @property
    def log_dir(self) -> Path:
        return self.base_dir / "logs"

    def ensure_dirs(self) -> None:
        for p in (self.base_dir, self.syncthing_home, self.log_dir):
            p.mkdir(parents=True, exist_ok=True)

    # ---- 低レベルの読み書き ----
    def _write_json(self, path: Path, payload: dict[str, Any], keep_backup: bool = True) -> None:
        """途中で電源が落ちても壊れないよう、別名で書いてから置き換える。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        if keep_backup and path.exists():
            # いま有る（=前回正常に読めた）ファイルを控えに回す
            try:
                if self._read_json_strict(path) is not None:
                    # 移動ではなく複写（本体が存在しない瞬間を作らない）
                    shutil.copyfile(path, path.with_name(path.name + ".bak"))
            except OSError:
                pass
        os.replace(tmp, path)

    @staticmethod
    def _read_json_strict(path: Path) -> dict[str, Any] | None:
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _read_json_recovering(self, path: Path, label: str) -> dict[str, Any] | None:
        """読めればその内容。壊れていれば退避して控えを試す。どちらも無ければ None。"""
        if not path.exists():
            return None
        data = self._read_json_strict(path)
        if data is not None:
            return data
        stamp = time.strftime("%Y%m%d-%H%M%S")
        broken = path.with_name(f"{path.name}.broken-{stamp}")
        try:
            os.replace(path, broken)
            note = f"{label}が壊れていたため {broken.name} に退避しました。"
        except OSError:
            note = f"{label}が壊れていて読めませんでした（退避にも失敗）。"
        bak = path.with_name(path.name + ".bak")
        data = self._read_json_strict(bak) if bak.exists() else None
        if data is not None:
            note += " 直前の控えから復元しました。"
        else:
            note += " 控えが無いので既定値で続行します。"
        log.warning(note)
        self.recovery_notes.append(note)
        return data

    # ---- 設定 ----
    def load_settings(self) -> Settings:
        raw = self._read_json_recovering(self.settings_path, "設定ファイル")
        settings = _from_dict(Settings, raw) if raw is not None else Settings()
        settings.validate()
        return settings

    def save_settings(self, settings: Settings) -> None:
        settings.validate()
        payload = asdict(settings)
        # 機密が紛れ込まないよう、決めた項目だけを書く（asdict は Settings の項目だけ）
        self._write_json(self.settings_path, payload)

    # ---- 機密 ----
    def load_secrets(self) -> Secrets:
        """API キーを読む。無い・壊れている場合は新しく作って保存する。

        API キーは起動のたびに環境変数で Syncthing へ渡すので、作り直しても支障はない。
        """
        raw = self._read_json_recovering(self.secrets_path, "機密ファイル")
        key = raw.get("api_key") if raw else None
        if not isinstance(key, str) or len(key) < 20:
            key = generate_api_key()
            secrets = Secrets(api_key=key)
            self.save_secrets(secrets)
        else:
            secrets = Secrets(api_key=key)
        register_secret(secrets.api_key)
        return secrets

    def save_secrets(self, secrets: Secrets) -> None:
        register_secret(secrets.api_key)
        # 機密は控え（.bak）を増やさない
        self._write_json(self.secrets_path, {"api_key": secrets.api_key}, keep_backup=False)

    # ---- 実行時情報 ----
    def load_runtime(self) -> RuntimeInfo:
        raw = self._read_json_strict(self.runtime_path) if self.runtime_path.exists() else None
        return _from_dict(RuntimeInfo, raw) if raw else RuntimeInfo()

    def save_runtime(self, info: RuntimeInfo) -> None:
        self._write_json(self.runtime_path, asdict(info), keep_backup=False)

    def clear_runtime(self) -> None:
        try:
            self.runtime_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("runtime.json を消せませんでした: %s", exc)


def generate_api_key() -> str:
    """推測できない API キー（URL に使える 43 文字）。"""
    return _secrets.token_urlsafe(32)
