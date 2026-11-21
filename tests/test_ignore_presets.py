"""ignore_presets: Unity などの除外プリセットと .stignore への反映。"""

from __future__ import annotations

from fake_client import FakeClient
from folderlink.core import ignore_presets as ip


def make_unity(root, name=""):
    base = root / name if name else root
    for d in ("Assets/Scripts", "Packages", "ProjectSettings", "Library/Cache", "Temp", "Logs"):
        (base / d).mkdir(parents=True, exist_ok=True)
    return base


# ---------------------------------------------------------------- プリセットの中身
def test_presets_exist_with_japanese_text():
    assert {"unity", "os_junk", "dev_cache"} <= set(ip.PRESETS)
    for p in ip.PRESETS.values():
        assert p.name and p.description


def test_unity_preset_covers_spec_folders_and_never_ignores_sources():
    lines = ip.unity_lines([""])
    for name in ("Library", "Temp", "Obj", "Logs", "Build", "Builds", ".vs"):
        assert f"/{name}" in lines
    for must_sync in ("Assets", "Packages", "ProjectSettings"):
        assert not any(must_sync in line for line in lines if not line.startswith("//"))
    # 判定でも確認: 再生成できるものは除外、ソースは同期
    for ignored in ("Library/ArtifactDB", "Temp/x.tmp", "Obj/Debug/a.dll", "Logs/Editor.log", "Build/game.exe",
                    "Builds/win/game.exe", ".vs/config.json", "UserSettings/Layouts/x.dwlt",
                    "Assembly-CSharp.csproj", "MyGame.sln"):
        assert ip.simple_match(lines, ignored), ignored
    for synced in ("Assets/Scenes/Main.unity", "Assets/Scenes/Main.unity.meta", "Packages/manifest.json",
                   "ProjectSettings/ProjectVersion.txt", "Assets/Plugins/Library/keep.dll",
                   "Assets/Build/data.asset", "Assets/Logs/readme.txt", "Assets/Temp/a.txt",
                   "README.md"):
        assert not ip.simple_match(lines, synced), synced


def test_detect_unity_projects(tmp_path):
    assert ip.detect_unity_projects(tmp_path) == []
    make_unity(tmp_path, "GameA")
    make_unity(tmp_path, "work/GameB")
    (tmp_path / "docs").mkdir()
    (tmp_path / "too" / "deep" / "down" / "here").mkdir(parents=True)
    make_unity(tmp_path, "too/deep/down/here/GameC")  # 深すぎるものは探さない
    assert ip.detect_unity_projects(tmp_path) == ["GameA", "work/GameB"]


def test_detect_root_project(tmp_path):
    make_unity(tmp_path)
    assert ip.is_unity_project(tmp_path)
    assert ip.detect_unity_projects(tmp_path) == [""]


def test_unity_lines_for_nested_projects_are_anchored(tmp_path):
    make_unity(tmp_path, "GameA")
    make_unity(tmp_path, "work/Game B")
    lines = ip.build_managed_lines(["unity"], None, tmp_path)
    assert "/GameA/Library" in lines and "/work/Game B/Library" in lines
    assert "/Library" not in lines
    assert ip.simple_match(lines, "GameA/Library/x")
    assert ip.simple_match(lines, "work/Game B/Temp/y")
    assert not ip.simple_match(lines, "GameA/Assets/Library/x")
    assert not ip.simple_match(lines, "Other/Library/x")  # Unity プロジェクトでない場所の Library は同期する


def test_unity_preset_without_detected_project_uses_root():
    lines = ip.build_managed_lines(["unity"], None, None)
    assert "/Library" in lines and "/Temp" in lines


def test_special_characters_in_project_path_are_escaped():
    assert "/My |[Game|]/Library" in ip.unity_lines(["My [Game]"])


def test_os_junk_and_unknown_preset():
    lines = ip.build_managed_lines(["os_junk", "no_such_preset"])
    assert "(?d)Thumbs.db" in lines and "(?d)desktop.ini" in lines
    assert ip.simple_match(lines, "photos/Thumbs.db") and ip.simple_match(lines, "docs/~$report.docx")
    assert not ip.simple_match(lines, "docs/report.docx")
    assert ip.build_managed_lines([]) == [] and ip.build_managed_lines(None) == []


def test_custom_lines_come_first_and_are_cleaned():
    lines = ip.build_managed_lines(["unity"], ["  *.blend1  ", "", "!/Library/keep.txt", "*.blend1",
                                               "#include other.txt", ip.BEGIN_MARK, "a\nb"])
    assert lines[:5] == ["// 自分で追加した除外", "*.blend1", "!/Library/keep.txt", "a", "b"]
    assert not any("#include" in line for line in lines)
    # 先に書いた行が勝つので、利用者の「除外の取り消し」がプリセットより優先される
    assert not ip.simple_match(lines, "Library/keep.txt")
    assert ip.simple_match(lines, "Library/other.txt")


# ---------------------------------------------------------------- .stignore への反映
def test_merge_into_empty():
    assert ip.merge_stignore([], ["/Library"]) == [ip.BEGIN_MARK, "/Library", ip.END_MARK]
    assert ip.merge_stignore([], []) == []


def test_merge_preserves_user_lines_and_replaces_managed_block():
    existing = ["// 自分のメモ", "*.bak", "", ip.BEGIN_MARK, "/Old", ip.END_MARK, "secret/"]
    merged = ip.merge_stignore(existing, ["/Library", "/Temp"])
    assert merged == [ip.BEGIN_MARK, "/Library", "/Temp", ip.END_MARK, "", "// 自分のメモ", "*.bak", "", "secret/"]
    # もう一度かけても増えない
    assert ip.merge_stignore(merged, ["/Library", "/Temp"]) == merged
    # プリセットを外すと、管理部分だけが消える
    assert ip.merge_stignore(merged, []) == ["// 自分のメモ", "*.bak", "", "secret/"]


def test_apply_to_folder_writes_file(tmp_path):
    make_unity(tmp_path)
    (tmp_path / ".stignore").write_bytes(b"*.bak\r\n")  # Windows の改行で書かれた既存ファイル
    merged = ip.apply_to_folder(tmp_path, ["unity", "os_junk"], ["*.psd~"])
    text = (tmp_path / ".stignore").read_text(encoding="utf-8")
    assert text.splitlines() == merged
    assert text.startswith(ip.BEGIN_MARK) and text.endswith("*.bak\n")
    assert "/Library" in merged and "*.psd~" in merged and "(?d)Thumbs.db" in merged
    assert not list(tmp_path.glob(".stignore.fl-tmp"))
    assert ip.read_stignore(tmp_path) == merged


def test_apply_nothing_to_folder_without_stignore_creates_no_file(tmp_path):
    assert ip.apply_to_folder(tmp_path, [], []) == []
    assert not (tmp_path / ".stignore").exists()
    assert ip.read_stignore(tmp_path) == []


def test_removing_presets_keeps_user_file(tmp_path):
    (tmp_path / ".stignore").write_text("*.bak\n", encoding="utf-8")
    ip.apply_to_folder(tmp_path, ["os_junk"])
    ip.apply_to_folder(tmp_path, [])
    assert (tmp_path / ".stignore").read_text(encoding="utf-8") == "*.bak\n"


def test_apply_via_api(tmp_path):
    make_unity(tmp_path)
    c = FakeClient()
    c.ignores["f1"] = ["*.bak"]
    merged = ip.apply_via_api(c, "f1", tmp_path, ["unity"])
    assert c.ignores["f1"] == merged
    assert merged[0] == ip.BEGIN_MARK and merged[-1] == "*.bak" and "/Library" in merged
