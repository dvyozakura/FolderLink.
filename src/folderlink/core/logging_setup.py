"""ログの設定。ファイルに出す内容からは、必ず機密（API キーなど）を消す。"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from folderlink.core.redact import RedactingFilter, redact

LOG_NAME = "folderlink.log"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(log_dir: Path, level: int = logging.INFO, also_stderr: bool = False) -> Path:
    """ログをファイル（1MB × 3 世代）に出す。戻り値はログファイルの場所。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / LOG_NAME
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_folderlink", False):
            root.removeHandler(handler)
            handler.close()
    file_handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    file_handler.addFilter(RedactingFilter())
    file_handler._folderlink = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
    if also_stderr:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(_FORMAT))
        stream.addFilter(RedactingFilter())
        stream._folderlink = True  # type: ignore[attr-defined]
        root.addHandler(stream)
    # 通信ライブラリの詳細ログは出さない（URL などが大量に出るため）
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    return path


def install_excepthook() -> None:
    """想定外の例外をログに残す（機密は消す）。クラッシュ報告を外部へ送ることはしない。"""
    log = logging.getLogger("folderlink.crash")

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.error("想定外のエラー", exc_info=(exc_type, exc, tb))

    sys.excepthook = hook


def read_log_tail(log_dir: Path, lines: int = 300) -> str:
    """FolderLink 自身のログの末尾（表示用。念のためもう一度機密を消す）。"""
    path = log_dir / LOG_NAME
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return redact("\n".join(text.splitlines()[-lines:]))
