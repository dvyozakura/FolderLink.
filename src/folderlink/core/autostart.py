"""Windows ログイン時の自動起動（HKCU\\...\\Run）。

- 書き込むのは「現在のユーザー」の Run キーだけ（管理者権限は不要、他のユーザーに影響しない）。
- 登録するコマンドは `"<FolderLink.exe のパス>" --minimized`。
- **開発中（EXE 化していない実行）では登録しない。** 開発用の python のパスを自動起動に書いて
  しまう事故を防ぐため、`enable()` は EXE 化された製品版でだけ動く。
- レジストリの操作は RegistryBackend に閉じ込めてあり、テストでは偽物に差し替える。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "FolderLink"
MINIMIZED_FLAG = "--minimized"


class AutoStartError(Exception):
    """自動起動の設定に失敗（文面は利用者に見せてよい日本語）。"""


class RegistryBackend(Protocol):
    def read(self, key: str, name: str) -> str | None: ...
    def write(self, key: str, name: str, value: str) -> None: ...
    def delete(self, key: str, name: str) -> None: ...


class WinRegBackend:
    """本物のレジストリ（HKEY_CURRENT_USER）。"""

    def read(self, key: str, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ) as handle:
                value, _kind = winreg.QueryValueEx(handle, name)
                return str(value)
        except FileNotFoundError:
            return None

    def write(self, key: str, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
            winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)

    def delete(self, key: str, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
                winreg.DeleteValue(handle, name)
        except FileNotFoundError:
            pass


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def build_command(executable: str | Path) -> str:
    """Run キーに書くコマンド。空白を含むパスでも動くよう、必ず引用符で囲む。"""
    path = str(executable)
    if '"' in path:
        raise AutoStartError("実行ファイルの場所に使えない文字が含まれています。")
    return f'"{path}" {MINIMIZED_FLAG}'


class AutoStart:
    def __init__(self, registry: RegistryBackend | None = None, executable: str | Path | None = None,
                 frozen: bool | None = None) -> None:
        self._registry = registry if registry is not None else WinRegBackend()
        self._frozen = is_frozen() if frozen is None else frozen
        self._executable = str(executable) if executable is not None else sys.executable

    @property
    def can_enable(self) -> bool:
        """自動起動を登録できる実行形態か（EXE 化された製品版だけ）。"""
        return self._frozen

    def expected_command(self) -> str:
        return build_command(self._executable)

    def current_command(self) -> str | None:
        try:
            return self._registry.read(RUN_KEY, VALUE_NAME)
        except OSError as exc:
            raise AutoStartError(f"自動起動の設定を読めませんでした（{type(exc).__name__}）。") from None

    def is_enabled(self) -> bool:
        """自動起動が登録されているか（登録先がこの EXE でなくても、登録があれば True）。"""
        try:
            return bool(self.current_command())
        except AutoStartError:
            return False

    def points_to_this_executable(self) -> bool:
        """登録されているコマンドが、いま動いている EXE を指しているか。"""
        try:
            current = self.current_command()
        except AutoStartError:
            return False
        return bool(current) and current.strip().lower() == self.expected_command().lower()

    def enable(self) -> str:
        if not self._frozen:
            raise AutoStartError(
                "開発中の実行では自動起動を登録しません。インストールした FolderLink から設定してください。"
            )
        command = self.expected_command()
        try:
            self._registry.write(RUN_KEY, VALUE_NAME, command)
        except OSError as exc:
            raise AutoStartError(f"自動起動を登録できませんでした（{type(exc).__name__}）。") from None
        return command

    def disable(self) -> None:
        try:
            self._registry.delete(RUN_KEY, VALUE_NAME)
        except OSError as exc:
            raise AutoStartError(f"自動起動を解除できませんでした（{type(exc).__name__}）。") from None

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self.enable()
        else:
            self.disable()


class MemoryRegistry:
    """メモリ上だけの偽レジストリ（テスト・画面の見本用。本物には触れない）。"""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail: OSError | None = None

    def read(self, key: str, name: str) -> str | None:
        if self.fail:
            raise self.fail
        return self.values.get((key, name))

    def write(self, key: str, name: str, value: str) -> None:
        if self.fail:
            raise self.fail
        self.values[(key, name)] = value

    def delete(self, key: str, name: str) -> None:
        if self.fail:
            raise self.fail
        self.values.pop((key, name), None)
