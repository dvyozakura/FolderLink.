"""config_store: 保存先、設定と機密の分離、壊れたファイルからの復旧。"""

from __future__ import annotations

import json
from pathlib import Path

from folderlink.core import config_store
from folderlink.core.config_store import ConfigStore, RuntimeInfo, Secrets, Settings


def test_data_dir_uses_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("FOLDERLINK_DATA_DIR", str(tmp_path / "x"))
    assert config_store.data_dir() == tmp_path / "x"


def test_data_dir_default_is_localappdata_folderlink(tmp_path, monkeypatch):
    monkeypatch.delenv("FOLDERLINK_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert config_store.data_dir() == tmp_path / "Local" / "FolderLink"


def test_tests_never_point_at_real_appdata(tmp_path):
    """conftest の自動フィクスチャで、保存先が一時フォルダになっていること。"""
    store = ConfigStore()
    assert tmp_path in store.base_dir.parents or store.base_dir.parent == tmp_path


def test_defaults_when_no_file(tmp_path):
    store = ConfigStore(tmp_path)
    s = store.load_settings()
    assert s == Settings()
    assert s.setup_completed is False
    assert s.versioning_days == 30
    assert store.recovery_notes == []


def test_settings_roundtrip_with_japanese_path(tmp_path):
    store = ConfigStore(tmp_path)
    s = Settings(theme="dark", folder_path=r"C:\Users\太郎\Desktop\共有フォルダ", folder_id="fl-abc",
                 peer_device_id="X" * 56, peer_name="PC-B", setup_completed=True,
                 ignore_presets=["unity"], custom_ignores=["*.tmp"])
    store.save_settings(s)
    assert ConfigStore(tmp_path).load_settings() == s
    # 日本語がそのまま読める形で保存されている
    assert "共有フォルダ" in store.settings_path.read_text(encoding="utf-8")


def test_secrets_are_separate_from_settings(tmp_path):
    store = ConfigStore(tmp_path)
    store.save_settings(Settings(folder_path="C:/x"))
    secrets = store.load_secrets()
    assert len(secrets.api_key) >= 40
    assert store.secrets_path != store.settings_path
    assert secrets.api_key not in store.settings_path.read_text(encoding="utf-8")
    assert json.loads(store.secrets_path.read_text(encoding="utf-8")) == {"api_key": secrets.api_key}
    # 設定を保存し直しても機密は紛れ込まない
    store.save_settings(store.load_settings())
    assert secrets.api_key not in store.settings_path.read_text(encoding="utf-8")


def test_api_key_is_stable_across_loads_and_random(tmp_path):
    a = ConfigStore(tmp_path / "a").load_secrets().api_key
    assert ConfigStore(tmp_path / "a").load_secrets().api_key == a
    b = ConfigStore(tmp_path / "b").load_secrets().api_key
    assert a != b


def test_secrets_repr_hides_key(tmp_path):
    secrets = ConfigStore(tmp_path).load_secrets()
    assert secrets.api_key not in repr(secrets)
    assert secrets.api_key not in str(secrets)
    assert secrets.api_key not in f"{secrets}"
    assert repr(Secrets("abcdefghijklmnopqrstuvwxyz")) == "Secrets(api_key=***)"


def test_broken_settings_is_set_aside_and_backup_used(tmp_path):
    store = ConfigStore(tmp_path)
    store.save_settings(Settings(theme="dark", folder_path="C:/first"))
    store.save_settings(Settings(theme="light", folder_path="C:/second"))  # first が .bak になる
    store.settings_path.write_text("{ これは JSON ではない", encoding="utf-8")

    store2 = ConfigStore(tmp_path)
    s = store2.load_settings()
    assert s.folder_path == "C:/first"  # 直前の控えから復元
    assert store2.recovery_notes and "退避" in store2.recovery_notes[0]
    broken = list(tmp_path.glob("settings.json.broken-*"))
    assert len(broken) == 1  # 壊れたファイルは消さずに残す
    # その後、普通に保存・読み込みできる
    store2.save_settings(s)
    assert ConfigStore(tmp_path).load_settings().folder_path == "C:/first"


def test_broken_settings_without_backup_falls_back_to_defaults(tmp_path):
    store = ConfigStore(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    store.settings_path.write_bytes(b"\x00\xff\x00 garbage")
    s = store.load_settings()
    assert s == Settings()
    assert "既定値" in store.recovery_notes[0]


def test_settings_file_that_is_not_an_object(tmp_path):
    store = ConfigStore(tmp_path)
    store.settings_path.write_text("[1, 2, 3]", encoding="utf-8")
    assert store.load_settings() == Settings()
    assert store.recovery_notes


def test_wrong_types_and_unknown_keys_are_ignored(tmp_path):
    store = ConfigStore(tmp_path)
    store.settings_path.write_text(json.dumps({
        "theme": "neon",                # 知らない値
        "versioning_days": "thirty",    # 型違い
        "setup_completed": "yes",       # 型違い
        "folder_path": 123,             # 型違い
        "ignore_presets": ["unity", 5], # 混ざりもの
        "mass_delete_threshold": -3,    # 範囲外
        "api_key": "should-not-be-here",
        "unknown": {"a": 1},
    }), encoding="utf-8")
    s = store.load_settings()
    assert s.theme == "system"
    assert s.versioning_days == 30
    assert s.setup_completed is False
    assert s.folder_path is None
    assert s.ignore_presets == ["unity"]
    assert s.mass_delete_threshold == 50
    assert not hasattr(s, "api_key")
    store.save_settings(s)
    assert "should-not-be-here" not in store.settings_path.read_text(encoding="utf-8")


def test_broken_secrets_are_regenerated(tmp_path):
    store = ConfigStore(tmp_path)
    first = store.load_secrets().api_key
    store.secrets_path.write_text("not json", encoding="utf-8")
    second = ConfigStore(tmp_path).load_secrets().api_key
    assert second and second != first
    assert ConfigStore(tmp_path).load_secrets().api_key == second


def test_too_short_api_key_is_replaced(tmp_path):
    store = ConfigStore(tmp_path)
    store.secrets_path.write_text(json.dumps({"api_key": "abc"}), encoding="utf-8")
    assert len(store.load_secrets().api_key) >= 40


def test_atomic_write_leaves_no_tmp_file(tmp_path):
    store = ConfigStore(tmp_path)
    store.save_settings(Settings())
    store.load_secrets()
    assert not list(tmp_path.glob("*.tmp"))


def test_runtime_roundtrip_and_clear(tmp_path):
    store = ConfigStore(tmp_path)
    assert store.load_runtime() == RuntimeInfo()
    store.save_runtime(RuntimeInfo(pid=1234, port=50123, started_at=1.5, device_id="ABC"))
    assert store.load_runtime() == RuntimeInfo(pid=1234, port=50123, started_at=1.5, device_id="ABC")
    store.clear_runtime()
    store.clear_runtime()  # 2回呼んでも落ちない
    assert store.load_runtime() == RuntimeInfo()


def test_broken_runtime_is_ignored(tmp_path):
    store = ConfigStore(tmp_path)
    store.runtime_path.write_text("{{{", encoding="utf-8")
    assert store.load_runtime() == RuntimeInfo()


def test_paths_are_under_base_dir(tmp_path):
    store = ConfigStore(tmp_path)
    store.ensure_dirs()
    for p in (store.settings_path, store.secrets_path, store.runtime_path, store.syncthing_home, store.log_dir):
        assert Path(tmp_path) in p.parents
    assert store.syncthing_home.is_dir() and store.log_dir.is_dir()
