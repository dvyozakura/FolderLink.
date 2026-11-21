"""ビルド用: アプリのアイコン（.ico、複数サイズ入り）を作る。画面は出さない。

使い方: python -m folderlink.devtools.make_icon <出力先.ico>
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 128, 256)


def build_ico(out: Path) -> Path:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QGuiApplication

    from folderlink.ui import icons

    _app = QGuiApplication.instance() or QGuiApplication(["make_icon"])
    images: list[tuple[int, bytes]] = []
    for size in SIZES:
        pix = icons.app_pixmap(size)
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pix.toImage().save(buf, "PNG")
        buf.close()
        images.append((size, bytes(data.data())))

    # ICO: ヘッダー + 項目（PNG をそのまま入れる。Windows Vista 以降で使える形式）
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(blobs))
        blobs += png
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(header + entries + blobs)
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "build/FolderLink.ico")
    print(build_ico(target))
