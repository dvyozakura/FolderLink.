"""結合テスト（調査用）: staggered バージョニングの実際の保持のされ方を見る。結果は -s で表示。"""

from __future__ import annotations

import os

from it_helpers import wait_until


def versions_tree(folder):
    root = folder / ".stversions"
    if not root.exists():
        return {}
    return {p.relative_to(root).as_posix(): p.stat().st_size for p in sorted(root.rglob("*")) if p.is_file()}


def test_probe_versioning_behaviour(new_pair):
    p = new_pair().start()
    p.wait_connected()
    a, b = p.a.folder, p.b.folder
    print("既定のフォルダ設定の versioning:", p.a.client.default_folder().get("versioning"))
    print("共有後の versioning:", p.a.client.get_folder(p.folder_id).get("versioning"))

    (a / "doc.txt").write_text("v1", encoding="utf-8")
    (a / "sub").mkdir()
    (a / "sub" / "data.bin").write_bytes(os.urandom(200_000))
    p.settle()
    assert versions_tree(a) == {} and versions_tree(b) == {}

    # A で上書き → B に過去版ができる（A にはできない）
    (a / "doc.txt").write_text("v2-longer", encoding="utf-8")
    p.settle()
    print("A で上書きしたあと  A:", versions_tree(a), " B:", versions_tree(b))

    # A で続けて 3 回上書き
    for i in range(3, 6):
        (a / "doc.txt").write_text(f"v{i}" + "x" * i, encoding="utf-8")
        p.settle()
    print("さらに3回上書き     B:", versions_tree(b))

    # A で削除 → B に削除前の版が残る
    os.remove(a / "sub" / "data.bin")
    p.settle()
    print("A で削除したあと    A:", versions_tree(a), " B:", versions_tree(b))

    api = p.b.client.folder_versions(p.folder_id)
    print("B の API の一覧:", api)

    # B で、削除されたファイルを API から復元
    path = "sub/data.bin" if "sub/data.bin" in api else next(k for k in api if k.endswith("data.bin"))
    errors = p.b.client.restore_versions(p.folder_id, {path: api[path][0]["versionTime"]})
    print("削除の復元の結果:", errors)
    wait_until(lambda: (b / "sub" / "data.bin").exists(), 30, desc="復元")
    p.settle()
    print("復元後 A にも戻ったか:", (a / "sub" / "data.bin").exists(), " B の過去版:", versions_tree(b))

    # B で、doc.txt を一番古い版に戻す（今の版はどうなるか）
    api = p.b.client.folder_versions(p.folder_id)
    key = next(k for k in api if k.endswith("doc.txt"))
    oldest = sorted(api[key], key=lambda v: v["versionTime"])[0]
    current_before = (b / "doc.txt").read_text(encoding="utf-8")
    errors = p.b.client.restore_versions(p.folder_id, {key: oldest["versionTime"]})
    print("上書きの復元の結果:", errors, " 復元前の内容:", current_before)
    wait_until(lambda: (b / "doc.txt").read_text(encoding="utf-8") != current_before, 30, desc="復元")
    print("復元後の内容:", (b / "doc.txt").read_text(encoding="utf-8"), " B の過去版:", versions_tree(b))
    p.settle()
    print("A の内容:", (a / "doc.txt").read_text(encoding="utf-8"), " A の過去版:", versions_tree(a))

    # 手で .stversions に置いたファイルは、API の一覧に出るか
    manual = b / ".stversions" / "manual~20261121-010203.txt"
    manual.write_text("manual", encoding="utf-8")
    api = p.b.client.folder_versions(p.folder_id)
    print("手で置いた版が一覧に出るか:", {k: v for k, v in api.items() if "manual" in k})
