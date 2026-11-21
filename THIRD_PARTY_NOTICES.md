# THIRD_PARTY_NOTICES（同梱・依存しているソフトウェア）

FolderLink 自体は MIT License です（[LICENSE](LICENSE)）。FolderLink のセットアップ（`FolderLink_Setup.exe`）とインストール先には、次のソフトウェアが含まれます。各ソフトウェアの著作権はそれぞれの作者にあり、それぞれのライセンスに従って配布しています。

版は、FolderLink 0.1.0 のビルドに使ったものです。インストール先の `licenses\` フォルダに、各パッケージに含まれていたライセンス文（と Python のライセンス文。OpenSSL・libffi などの表記を含む）を集めてあります。

## 同期エンジン

| ソフトウェア | 版 | ライセンス | 入手先 |
|---|---|---|---|
| Syncthing（Windows amd64） | v2.1.7 | Mozilla Public License 2.0 | https://github.com/syncthing/syncthing （ソース: https://github.com/syncthing/syncthing/tree/v2.1.7 、配布物: https://github.com/syncthing/syncthing/releases/tag/v2.1.7 ） |

- 公式リリースの `syncthing-windows-amd64-v2.1.7.zip` から取り出した `syncthing.exe` を**改変せずに**、インストール先の `syncthing\` に置いています。ZIP の SHA-256 は公式の `sha256sum.txt.asc` と照合しています。
- 同じフォルダに、Syncthing のライセンス文（`LICENSE.txt`）、作者一覧（`AUTHORS.txt`）、`README.txt` を一緒に入れています。
- MPL-2.0 により、Syncthing のソースコードは上の入手先から誰でも入手できます。FolderLink は Syncthing のファイルを変更していないため、FolderLink 側で公開すべき Syncthing の変更点はありません。
- 新しい版への差し替え方は [README.md](README.md) の「Syncthing の差し替え方」にあります。
- FolderLink は Syncthing プロジェクトの公式ソフトウェアではありません。「Syncthing」は Syncthing Foundation の名称です。

## 画面（GUI）

| ソフトウェア | 版 | ライセンス | 入手先 |
|---|---|---|---|
| Qt for Python（PySide6） | 6.12.0 | GNU LGPL v3（Qt for Python） | https://wiki.qt.io/Qt_for_Python 、ソース: https://code.qt.io/cgit/pyside/pyside-setup.git |
| Shiboken6 | 6.12.0 | GNU LGPL v3 | 同上 |
| Qt 6（Core・Gui・Widgets・Network ほか、PySide6 に含まれるライブラリ） | 6.12.0 | GNU LGPL v3 | https://www.qt.io/ 、ソース: https://download.qt.io/official_releases/qt/ |
| Mesa llvmpipe（`opengl32sw.dll`。PySide6 に含まれるソフトウェア描画用） | PySide6 6.12.0 に同梱の版 | MIT License | https://mesa3d.org/ |

- Qt と PySide6 は、改変せずに**動的リンク**（DLL・`.pyd`）として使っています。インストール先の `_internal\PySide6\` と `_internal\shiboken6\` にあり、利用者が LGPL v3 に従って別の互換な版に差し替えることができます。
- LGPL v3 の全文: https://www.gnu.org/licenses/lgpl-3.0.html （GPL v3: https://www.gnu.org/licenses/gpl-3.0.html ）

## 通信・実行環境

| ソフトウェア | 版 | ライセンス | 入手先 |
|---|---|---|---|
| Python | 3.13 | Python Software Foundation License 2.0 | https://www.python.org/ |
| requests | 2.34.2 | Apache License 2.0 | https://github.com/psf/requests |
| urllib3 | 2.8.0 | MIT License | https://github.com/urllib3/urllib3 |
| idna | 3.20 | BSD 3-Clause License | https://github.com/kjd/idna |
| charset-normalizer | 3.5.2 | MIT License | https://github.com/jawah/charset_normalizer |
| certifi | 2026.10.23 | Mozilla Public License 2.0 | https://github.com/certifi/python-certifi |
| OpenSSL（`libssl-3.dll`・`libcrypto-3.dll`。Python に含まれる） | 3.x | Apache License 2.0 | https://www.openssl.org/ |
| libffi（Python に含まれる） | 3.x | MIT License | https://sourceware.org/libffi/ |
| setuptools（PyInstaller が一部を同梱） | 84.2.1 | MIT License | https://github.com/pypa/setuptools |
| PyInstaller のブートローダー（`FolderLink.exe` の起動部分） | 6.22.3 | GPL v2 + ブートローダー例外（作ったプログラムを任意のライセンスで配布可） | https://pyinstaller.org/ |
| Microsoft Visual C++ ランタイム・Universal CRT（`VCRUNTIME140*.dll`、`ucrtbase.dll`、`api-ms-win-*.dll`） | — | Microsoft の再頒布可能コードのライセンス | https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist |

## 開発・ビルドだけに使うもの（配布物には含まれない）

pytest（MIT）、Inno Setup（Inno Setup License）、PyInstaller 本体（GPL v2 + 例外）、pefile（MIT）、altgraph（MIT）、pywin32-ctypes（BSD 3-Clause）など。

---

この一覧は FolderLink の作者が配布物の中身から作成したもので、正確さには注意していますが、各ソフトウェアのライセンス本文が優先します。誤りに気づいた場合は、各ソフトウェアの入手先にある正式なライセンス文を参照してください。
