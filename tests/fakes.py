"""テスト用の偽物（HTTP セッションなど）。"""

from __future__ import annotations

import json
from typing import Any, Callable

import requests


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, text: str | None = None) -> None:
        self.status_code = status
        if text is not None:
            self.text = text
        elif body is None:
            self.text = ""
        else:
            self.text = json.dumps(body)

    def json(self) -> Any:
        return json.loads(self.text)


class FakeSession:
    """requests.Session の代わり。呼び出しを記録し、決めた応答を返す。

    routes: {(METHOD, path): 応答 または 応答のリスト（順に消費）または callable(call)->応答}
    応答に例外のインスタンスを置くと、その例外を投げる。
    """

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        self.routes: dict[tuple[str, str], Any] = dict(routes or {})
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.trust_env = True

    def add(self, method: str, path: str, response: Any) -> None:
        self.routes[(method.upper(), path)] = response

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        path = "/" + url.split("/", 3)[3]
        call = {"method": method.upper(), "url": url, "path": path, **kwargs}
        self.calls.append(call)
        key = (method.upper(), path)
        if key not in self.routes:
            return FakeResponse(404, text="404 page not found")
        entry = self.routes[key]
        if isinstance(entry, list):
            entry = entry.pop(0) if len(entry) > 1 else entry[0]
        if callable(entry) and not isinstance(entry, (FakeResponse, BaseException)):
            entry = entry(call)
        if isinstance(entry, BaseException):
            raise entry
        if isinstance(entry, FakeResponse):
            return entry
        return FakeResponse(200, entry)

    def close(self) -> None:
        self.closed = True

    def count(self, method: str, path: str) -> int:
        return sum(1 for c in self.calls if c["method"] == method.upper() and c["path"] == path)

    def last(self, method: str, path: str) -> dict[str, Any]:
        for c in reversed(self.calls):
            if c["method"] == method.upper() and c["path"] == path:
                return c
        raise AssertionError(f"呼ばれていません: {method} {path}")


def conn_error(text: str = "refused") -> requests.exceptions.ConnectionError:
    return requests.exceptions.ConnectionError(text)


def timeout_error(text: str = "timed out") -> requests.exceptions.ReadTimeout:
    return requests.exceptions.ReadTimeout(text)


Responder = Callable[[dict[str, Any]], Any]
