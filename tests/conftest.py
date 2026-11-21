"""テスト共通の設定。

安全ルール: テストが書き込んでよいのは pytest の一時フォルダと、プロジェクト内の
`_sandbox/` の下だけ。製品版の保存先（%LOCALAPPDATA%\\FolderLink）を誤って使わない
ように、環境変数で保存先を必ず差し替える。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = PROJECT_ROOT / "_sandbox"

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _isolate_app_dirs(tmp_path, monkeypatch):
    """どのテストでも、設定の保存先を一時フォルダへ向ける。"""
    monkeypatch.setenv("FOLDERLINK_DATA_DIR", str(tmp_path / "folderlink_data"))
    yield


@pytest.fixture(autouse=True)
def _never_write_real_registry(monkeypatch):
    """安全ルール: どのテストも、このPCのレジストリ（自動起動など）に書き込まない。"""
    try:
        import winreg
    except ImportError:
        yield
        return

    def blocked(*args, **kwargs):
        raise AssertionError("テストが本物のレジストリに書き込もうとしました")

    for name in ("CreateKey", "CreateKeyEx", "SetValue", "SetValueEx", "DeleteValue", "DeleteKey", "DeleteKeyEx"):
        monkeypatch.setattr(winreg, name, blocked)
    yield


@pytest.fixture
def sandbox() -> Path:
    SANDBOX.mkdir(exist_ok=True)
    return SANDBOX
