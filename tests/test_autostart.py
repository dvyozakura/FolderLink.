"""autostart: 自動起動（HKCU の Run）のオン/オフ。**本物のレジストリには触れない**（偽物で確認）。"""

from __future__ import annotations

import sys

import pytest

from folderlink.core import autostart as au
from folderlink.core.autostart import AutoStart, AutoStartError, MemoryRegistry

EXE = r"C:\Program Files\FolderLink\FolderLink.exe"


@pytest.fixture(autouse=True)
def forbid_real_registry(monkeypatch):
    """このテストの間、本物のレジストリを開こうとしたら即失敗させる（安全ルール）。"""
    def boom(*args, **kwargs):
        raise AssertionError("テストが本物のレジストリに触れようとしました")

    try:
        import winreg
    except ImportError:
        yield
        return
    for name in ("OpenKey", "OpenKeyEx", "CreateKey", "CreateKeyEx", "SetValueEx", "DeleteValue",
                 "QueryValueEx"):
        monkeypatch.setattr(winreg, name, boom)
    yield


def test_registers_under_hkcu_run_with_quoted_path_and_minimized_flag():
    reg = MemoryRegistry()
    a = AutoStart(reg, EXE, frozen=True)
    assert a.is_enabled() is False and a.current_command() is None
    command = a.enable()
    assert command == f'"{EXE}" --minimized'
    assert reg.values == {(r"Software\Microsoft\Windows\CurrentVersion\Run", "FolderLink"): command}
    assert a.is_enabled() is True and a.points_to_this_executable() is True


def test_disable_removes_only_our_value():
    reg = MemoryRegistry()
    reg.values[(au.RUN_KEY, "OneDrive")] = "onedrive.exe /background"
    a = AutoStart(reg, EXE, frozen=True)
    a.enable()
    a.disable()
    a.disable()  # 2回呼んでも落ちない
    assert reg.values == {(au.RUN_KEY, "OneDrive"): "onedrive.exe /background"}
    assert a.is_enabled() is False


def test_set_enabled_toggle():
    reg = MemoryRegistry()
    a = AutoStart(reg, EXE, frozen=True)
    a.set_enabled(True)
    assert a.is_enabled()
    a.set_enabled(False)
    assert not a.is_enabled()


def test_development_run_never_registers():
    """EXE 化していない実行（開発中・テスト中）では、登録を断る。"""
    reg = MemoryRegistry()
    a = AutoStart(reg, sys.executable, frozen=False)
    assert a.can_enable is False
    with pytest.raises(AutoStartError) as ei:
        a.enable()
    assert "開発中" in str(ei.value)
    assert reg.values == {}
    a.disable()  # 解除は開発中でもできる（残っていた登録を消せるように）


def test_default_is_not_frozen_in_tests():
    assert au.is_frozen() is False
    assert AutoStart(MemoryRegistry()).can_enable is False


def test_detects_registration_pointing_elsewhere():
    reg = MemoryRegistry()
    reg.values[(au.RUN_KEY, au.VALUE_NAME)] = r'"D:\old\FolderLink.exe" --minimized'
    a = AutoStart(reg, EXE, frozen=True)
    assert a.is_enabled() is True
    assert a.points_to_this_executable() is False
    a.enable()  # 登録し直すと、今の EXE を指す
    assert a.points_to_this_executable() is True


def test_path_with_quote_is_rejected():
    with pytest.raises(AutoStartError):
        au.build_command('C:\\bad"path\\FolderLink.exe')


def test_registry_errors_become_friendly_errors():
    reg = MemoryRegistry()
    a = AutoStart(reg, EXE, frozen=True)
    reg.fail = PermissionError("denied")
    with pytest.raises(AutoStartError):
        a.enable()
    with pytest.raises(AutoStartError):
        a.disable()
    with pytest.raises(AutoStartError):
        a.current_command()
    assert a.is_enabled() is False and a.points_to_this_executable() is False


def test_real_backend_is_lazy_and_blocked_here():
    """本物のバックエンドは、呼ばれるまでレジストリを開かない。呼べばこのテストでは必ず失敗する。"""
    a = AutoStart(None, EXE, frozen=True)  # 作るだけなら何も起きない
    if sys.platform == "win32":
        with pytest.raises(AssertionError):
            a.enable()
