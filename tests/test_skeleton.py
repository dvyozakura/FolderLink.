"""骨組みの確認: パッケージが読み込めて、バージョンが出る。"""

import folderlink
from folderlink.__main__ import main


def test_package_imports():
    assert folderlink.APP_NAME == "FolderLink"
    assert folderlink.__version__


def test_version_flag(capsys):
    assert main(["--version"]) == 0
    assert "FolderLink" in capsys.readouterr().out
