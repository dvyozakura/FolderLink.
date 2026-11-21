"""ビルド用: 同梱するパッケージのライセンス文を、配布物の licenses/ に集める（ネットには接続しない）。

使い方: python -m folderlink.devtools.collect_licenses <出力先フォルダ>
"""

from __future__ import annotations

import shutil
import sys
from importlib import metadata
from pathlib import Path

#: 配布物に含まれる（PyInstaller が同梱する）パッケージ
BUNDLED = ["PySide6", "PySide6_Essentials", "shiboken6", "requests", "urllib3", "idna",
           "charset_normalizer", "certifi", "setuptools"]


def collect(out: Path) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name in BUNDLED:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        for f in dist.files or []:
            parts = [p.lower() for p in Path(str(f)).parts]
            base = parts[-1]
            if "licenses" in parts or base.startswith(("license", "copying", "notice")):
                src = Path(str(dist.locate_file(f)))
                if src.is_file():
                    dst = out / f"{dist.metadata['Name']}-{dist.version}" / src.name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
                    written.append(str(dst.relative_to(out)))
    # Python 本体（PSF。OpenSSL・libffi などの表記も含む）
    py_license = Path(sys.base_prefix) / "LICENSE.txt"
    if py_license.is_file():
        dst = out / f"Python-{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(py_license, dst)
        written.append(str(dst.relative_to(out)))
    # 人が置いたライセンス文（例: Qt の LGPL v3 / GPL v3 の全文。wheel に入っていないため）
    extra = Path(__file__).resolve().parents[3] / "vendor" / "licenses"
    if extra.is_dir():
        for src in sorted(extra.iterdir()):
            if src.is_file():
                dst = out / "extra" / src.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                written.append(str(dst.relative_to(out)))
    return written


if __name__ == "__main__":
    for line in collect(Path(sys.argv[1] if len(sys.argv) > 1 else "dist/FolderLink/licenses")):
        print(line)
