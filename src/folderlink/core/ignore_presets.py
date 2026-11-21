"""除外プリセットと `.stignore` への反映。

`.stignore` の決まり（公式文書 https://docs.syncthing.net/users/ignoring.html）:
- フォルダ直下の `.stignore` に書く。**このファイル自体は同期されない**ので、PC ごとに反映が必要。
- 上から順に見て、最初に合った行で決まる。先頭 `/` はフォルダ直下だけ、`//` で始まる行はコメント。

FolderLink は `.stignore` の中に「管理部分」を作り、そこだけを書き換える。
利用者が自分で書いた行（管理部分の外）には触れない。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from folderlink.core.api_client import ApiClient

STIGNORE_NAME = ".stignore"
BEGIN_MARK = "// >>> FolderLink 管理部分（ここから下は自動で書き換わります。手で編集しないでください）"
END_MARK = "// <<< FolderLink 管理部分（ここまで）"

#: Unity プロジェクトの直下で、同期しないもの（再生成できるもの・PC ごとに違うもの）
UNITY_IGNORED_DIRS = ("Library", "Temp", "Obj", "Logs", "Build", "Builds", "UserSettings",
                      "MemoryCaptures", "Recordings", ".vs", ".gradle", ".utmp")
UNITY_IGNORED_FILES = ("*.csproj", "*.sln", "*.slnx", "*.suo", "*.user", "*.userprefs", "*.pidb",
                       "*.booproj", "*.unityproj", "sysinfo.txt", "mono_crash.*")
#: Unity で必ず同期するもの（除外に入れてはいけない）
UNITY_SYNCED_DIRS = ("Assets", "Packages", "ProjectSettings")


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    description: str
    lines: tuple[str, ...]  # 固定の行（Unity は見つけたプロジェクトごとに組み立てる）


PRESETS: dict[str, Preset] = {
    "unity": Preset(
        key="unity",
        name="Unity プロジェクト",
        description=(
            "Library・Temp・Obj・Logs・Build・Builds・UserSettings・.vs など、Unity が作り直せるフォルダを"
            "同期しません。Assets・Packages・ProjectSettings は同期します。"
        ),
        lines=(),
    ),
    "os_junk": Preset(
        key="os_junk",
        name="Windows / Mac の一時ファイル",
        description="Thumbs.db・desktop.ini・.DS_Store、Office の編集中ファイル（~$ で始まるもの）を同期しません。",
        lines=("(?d)Thumbs.db", "(?d)ehthumbs.db", "(?d)desktop.ini", "(?d).DS_Store", "(?d)~$*"),
    ),
    "dev_cache": Preset(
        key="dev_cache",
        name="開発ツールのキャッシュ",
        description=".vs・node_modules・__pycache__・.gradle など、作り直せるキャッシュを同期しません。",
        lines=(".vs", "node_modules", "__pycache__", ".gradle", "*.pyc"),
    ),
}


def is_unity_project(path: str | Path) -> bool:
    """Assets と ProjectSettings の両方があれば Unity プロジェクトとみなす。"""
    p = Path(path)
    try:
        return (p / "Assets").is_dir() and (p / "ProjectSettings").is_dir()
    except OSError:
        return False


def detect_unity_projects(root: str | Path, max_depth: int = 3, limit: int = 50) -> list[str]:
    """root 以下（深さ max_depth まで）の Unity プロジェクトを探す。

    返すのは root からの相対パス（区切りは /）。root 自身がプロジェクトなら ""。
    プロジェクトの中はそれ以上たどらない。
    """
    root = Path(root)
    found: list[str] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and len(found) < limit:
        current, depth = stack.pop()
        if is_unity_project(current):
            rel = current.relative_to(root).as_posix()
            found.append("" if rel == "." else rel)
            continue
        if depth >= max_depth:
            continue
        try:
            children = [e for e in os.scandir(current) if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for entry in children:
            if entry.name.startswith(".") or entry.name in ("node_modules",):
                continue
            stack.append((Path(entry.path), depth + 1))
    return sorted(found)


def _escape(segment: str) -> str:
    """パスの中の、パターンとして特別な文字を打ち消す（Windows の `.stignore` では | で打ち消す）。"""
    out = []
    for ch in segment:
        if ch in "*?[]{}|":
            out.append("|" + ch)
        else:
            out.append(ch)
    return "".join(out)


def unity_lines(project_rel_paths: list[str]) -> list[str]:
    """見つかった Unity プロジェクトごとの除外行。場所を固定（先頭 /）して、
    Assets の中の同名フォルダ（例: Assets/Foo/Library）を巻き込まないようにする。"""
    lines: list[str] = []
    for rel in project_rel_paths:
        prefix = "/" + "/".join(_escape(seg) for seg in rel.split("/") if seg) if rel else ""
        lines.append(f"// Unity プロジェクト: {rel or '（フォルダ直下）'}")
        for name in UNITY_IGNORED_DIRS:
            lines.append(f"{prefix}/{name}")
        for pattern in UNITY_IGNORED_FILES:
            lines.append(f"{prefix}/{pattern}")
    return lines


def clean_custom_lines(lines: list[str] | None) -> list[str]:
    """利用者が画面で足した行を整える（空行・改行・管理用の目印・#include を取り除く）。"""
    result: list[str] = []
    for raw in lines or []:
        if not isinstance(raw, str):
            continue
        for line in raw.replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line or line in (BEGIN_MARK, END_MARK):
                continue
            if line.lower().startswith("#include"):
                continue  # 別ファイルの読み込みは、無いとフォルダ全体がエラーになるので受け付けない
            if line not in result:
                result.append(line)
    return result


def build_managed_lines(preset_keys: list[str] | None, custom_lines: list[str] | None = None,
                        folder: str | Path | None = None) -> list[str]:
    """管理部分に入れる行を組み立てる。

    順番: 利用者が足した行 → プリセット（先に合った行が勝つので、利用者の指定を優先できる）。
    """
    lines: list[str] = []
    custom = clean_custom_lines(custom_lines)
    if custom:
        lines.append("// 自分で追加した除外")
        lines.extend(custom)
    for key in preset_keys or []:
        preset = PRESETS.get(key)
        if preset is None:
            continue
        if key == "unity":
            projects = detect_unity_projects(folder) if folder is not None else []
            if not projects:
                projects = [""]  # 見つからなければ、フォルダ直下を Unity プロジェクトとして扱う
            lines.extend(unity_lines(projects))
        else:
            lines.append(f"// {preset.name}")
            lines.extend(preset.lines)
    return lines


def merge_stignore(existing_lines: list[str], managed_lines: list[str]) -> list[str]:
    """既存の `.stignore` の行に、管理部分を差し込む（管理部分の外は変えない）。

    管理部分は**先頭**に置く。既存の管理部分があれば置き換える。
    管理部分が空なら、目印ごと取り除く。
    """
    outside: list[str] = []
    inside = False
    for line in existing_lines:
        stripped = line.strip()
        if stripped == BEGIN_MARK:
            inside = True
            continue
        if stripped == END_MARK:
            inside = False
            continue
        if not inside:
            outside.append(line.rstrip("\r\n"))
    while outside and not outside[0].strip():
        outside.pop(0)
    if not managed_lines:
        return outside
    merged = [BEGIN_MARK, *managed_lines, END_MARK]
    if outside:
        merged.append("")
        merged.extend(outside)
    return merged


def read_stignore(folder: str | Path) -> list[str]:
    path = Path(folder) / STIGNORE_NAME
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except FileNotFoundError:
        return []


def write_stignore(folder: str | Path, lines: list[str]) -> None:
    """`.stignore` を書く（別名で書いてから置き換え）。空になるなら何も作らない／既存は空にする。"""
    path = Path(folder) / STIGNORE_NAME
    if not lines and not path.exists():
        return
    tmp = path.with_name(STIGNORE_NAME + ".fl-tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(tmp, path)


def apply_to_folder(folder: str | Path, preset_keys: list[str] | None,
                    custom_lines: list[str] | None = None) -> list[str]:
    """フォルダの `.stignore` に反映する（エンジンにフォルダを登録する**前**に使う）。書いた行を返す。"""
    managed = build_managed_lines(preset_keys, custom_lines, folder)
    merged = merge_stignore(read_stignore(folder), managed)
    write_stignore(folder, merged)
    return merged


def apply_via_api(client: ApiClient, folder_id: str, folder: str | Path, preset_keys: list[str] | None,
                  custom_lines: list[str] | None = None) -> list[str]:
    """動いているフォルダに反映する（エンジン経由。すぐに読み直される）。書いた行を返す。"""
    managed = build_managed_lines(preset_keys, custom_lines, folder)
    merged = merge_stignore(client.get_ignores(folder_id), managed)
    client.set_ignores(folder_id, merged)
    return merged


def simple_match(lines: list[str], rel_path: str) -> bool:
    """除外されるかの簡易判定（プレビュー・テスト用。本物の判定は Syncthing が行う）。

    対応: 先頭 /、*・?、(?d)(?i) の前置き、! による取り消し、// コメント、
    「フォルダが除外されたら中身も除外」。`**`・[]・{} は簡易対応。
    """
    import fnmatch

    parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
    candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        negate = False
        changed = True
        while changed:
            changed = False
            for prefix in ("(?d)", "(?i)"):
                if line.startswith(prefix):
                    line = line[len(prefix):]
                    changed = True
            if line.startswith("!"):
                negate = True
                line = line[1:]
                changed = True
        line = line.replace("|", "")
        anchored = line.startswith("/")
        pattern = line.strip("/").lower()
        if not pattern:
            continue
        for cand in candidates:
            cand_l = cand.lower()
            if anchored:
                hit = fnmatch.fnmatchcase(cand_l, pattern)
            else:
                segs = cand_l.split("/")
                n = pattern.count("/") + 1
                hit = fnmatch.fnmatchcase("/".join(segs[-n:]), pattern)
            if hit:
                return not negate
    return False
