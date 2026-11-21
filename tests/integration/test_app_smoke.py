"""結合テスト: アプリ本体（python -m folderlink）を、画面を出さずに起動→エンジン確認→停止まで通す。"""

from __future__ import annotations

import os
import subprocess
import sys

from it_helpers import PROJECT_ROOT, syncthing_pids
from folderlink.core.syncthing_manager import find_free_port


def run_app(data_dir, *extra: str, timeout: int = 180) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["FOLDERLINK_DATA_DIR"] = str(data_dir)
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "folderlink", *extra],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )


def test_app_smoke_test_starts_and_stops_engine(workdir, binary, foreign_pids_at_start):
    before = syncthing_pids()
    data = workdir / "data"
    listen = f"tcp://127.0.0.1:{find_free_port()}"
    proc = run_app(data, "--smoke-test", "--isolated", f"--listen-address={listen}")
    print(proc.stdout, proc.stderr)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    result = (data / "smoke_result.txt").read_text(encoding="utf-8")
    assert "code=0" in result
    # 設定・鍵・ログは、指定した _sandbox/ 内のフォルダにだけ作られる
    assert (data / "syncthing" / "config.xml").is_file()
    assert (data / "secrets.json").is_file()
    assert (data / "logs" / "folderlink.log").is_file()
    # ログに API キーが出ていない
    import json

    key = json.loads((data / "secrets.json").read_text(encoding="utf-8"))["api_key"]
    for log_file in (data / "logs").iterdir():
        assert key not in log_file.read_text(encoding="utf-8", errors="replace"), log_file.name
    # エンジンは残っていない。実行時情報も片づいている
    assert syncthing_pids() - before == set()
    assert not (data / "runtime.json").exists()
    assert foreign_pids_at_start <= syncthing_pids()


def test_app_version_flag(workdir):
    proc = run_app(workdir / "data", "--version", timeout=60)
    assert proc.returncode == 0 and "FolderLink" in proc.stdout
    assert not (workdir / "data").exists()  # バージョン表示だけなら何も作らない
