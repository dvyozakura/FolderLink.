"""機密（API キーなど）をログ・例外メッセージ・画面に出さないための仕組み。

使い方:
    register_secret(api_key)      # 機密を登録
    redact("... api_key ...")     # 登録済みの機密を *** に置き換えた文字列を返す
    logging のハンドラーに RedactingFilter を付けると、ログの本文と引数も置き換わる
"""

from __future__ import annotations

import logging
import re
import threading

MASK = "***"

_lock = threading.Lock()
_secrets: set[str] = set()

# 登録し忘れに備えた保険: それらしい形のものも隠す
_PATTERNS = [
    re.compile(r"(?i)(x-api-key\s*[:=]\s*)([^\s,;'\"}]+)"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s,;'\"}]+)"),
    re.compile(r"(?i)(STGUIAPIKEY\s*=\s*)([^\s,;'\"}]+)"),
    re.compile(r"(?i)(--gui-apikey[= ])([^\s,;'\"}]+)"),
    re.compile(r"(?i)(<apikey>)([^<]+)(?=</apikey>)"),
    re.compile(r"(?i)(\"api_?key\"\s*:\s*\")([^\"]+)"),
]


def register_secret(value: str | None) -> None:
    """機密として扱う文字列を登録する。短すぎるもの（誤置換のもと）は無視。"""
    if value and len(value) >= 8:
        with _lock:
            _secrets.add(value)


def redact(text: object) -> str:
    """文字列の中の機密を *** に置き換える。"""
    s = text if isinstance(text, str) else str(text)
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for sec in secrets:
        if sec in s:
            s = s.replace(sec, MASK)
    for pat in _PATTERNS:
        s = pat.sub(lambda m: m.group(1) + MASK, s)
    return s


class RedactingFilter(logging.Filter):
    """ログレコードの本文・引数・例外から機密を消すフィルター。

    ロガーではなく**ハンドラー**に付けること（子ロガーから伝わってきたレコードにも効く）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # 書式が合わないログでも落とさない
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info:
            # 例外の文面にも機密が入り得るので、先に文字列にしてから消す
            formatter = logging.Formatter()
            record.exc_text = redact(formatter.formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True
