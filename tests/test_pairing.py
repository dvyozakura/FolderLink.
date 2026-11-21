"""pairing: デバイス ID の検証、登録、承認待ち、フォルダ共有、失敗の扱い。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fake_client import MY_ID, FakeClient
from folderlink.core import pairing as pg
from folderlink.core.api_client import ApiAuthError, ApiBadRequestError, ApiConnectionError, ApiTimeoutError
from folderlink.core.pairing import Pairing

# 公式文書（dev/device-ids）に載っている例。チェック文字つきの正しい ID。
DOC_ID = "MFZWI3D-BONSGYC-YLTMRWG-C43ENR5-QXGZDMM-FZWI3DP-BONSGYY-LTMRWAD"
# 別の正しい ID を作る: 52 文字からチェック文字を計算させる
PEER_ID = pg.normalize_device_id("P56IOI7MZJNU2IQGDREYDM2MGTMGL3BXNPQ6W5BTBBZ4TJXZWICQ")


# ---------------------------------------------------------------- デバイス ID
def test_documented_example_id_passes_checksum():
    assert pg.normalize_device_id(DOC_ID) == DOC_ID


def test_known_real_world_id_checksum():
    """Syncthing の REST 文書に載っている myID の例でも検算が合う。"""
    real = "P56IOI7-MZJNU2Y-IQGDREY-DM2MGTI-MGL3BXN-PQ6W5BM-TBBZ4TJ-XZWICQ2"
    assert pg.normalize_device_id(real) == real
    assert PEER_ID == real  # 52 文字から補ったチェック文字が、実物と一致する


@pytest.mark.parametrize("text", [
    DOC_ID.lower(),
    DOC_ID.replace("-", ""),
    DOC_ID.replace("-", " "),
    "  " + DOC_ID + "\r\n",
    DOC_ID[:30] + "\n" + DOC_ID[30:],
])
def test_tolerant_input_forms(text):
    assert pg.normalize_device_id(text) == DOC_ID


def test_lookalike_characters_are_substituted():
    # O と I を 0 と 1 で打っても通る（Syncthing と同じ読み替え）
    typed = DOC_ID.replace("O", "0").replace("I", "1")
    assert typed != DOC_ID
    assert pg.normalize_device_id(typed) == DOC_ID


def test_52_char_form_gets_check_digits():
    raw = DOC_ID.replace("-", "")
    blocks = [raw[i:i + 14] for i in range(0, 56, 14)]
    without = "".join(b[:13] for b in blocks)
    assert len(without) == 52
    assert pg.normalize_device_id(without) == DOC_ID


@pytest.mark.parametrize("text,reason", [
    ("", "empty"),
    ("   ", "empty"),
    (None, "empty"),
    ("ABC", "length"),
    (DOC_ID + "A", "length"),
    (DOC_ID[:-1], "length"),
    (DOC_ID.replace("M", "9", 1), "chars"),
    ("日本語のID", "chars"),
    (DOC_ID[:-1] + ("A" if DOC_ID[-1] != "A" else "B"), "checksum"),
    ("A" + DOC_ID[1:], "checksum"),
])
def test_invalid_ids(text, reason):
    with pytest.raises(pg.DeviceIdError) as ei:
        pg.normalize_device_id(text)
    assert ei.value.reason == reason
    assert str(ei.value)  # 日本語の説明がある
    assert pg.is_valid_device_id(text) is False


def test_single_typo_is_always_caught():
    """1文字の打ち間違いは、チェック文字で必ず見つかる。"""
    raw = DOC_ID.replace("-", "")
    for i in range(len(raw)):
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567":
            if ch == raw[i]:
                continue
            assert not pg.is_valid_device_id(raw[:i] + ch + raw[i + 1:]), (i, ch)


def test_own_id_is_rejected():
    p = Pairing(FakeClient(), MY_ID)
    with pytest.raises(pg.DeviceIdError) as ei:
        p.check_peer_id(MY_ID.lower())
    assert ei.value.reason == "self"
    assert p.check_peer_id(PEER_ID) == PEER_ID


# ---------------------------------------------------------------- 登録
def test_add_device_uses_defaults_template():
    c = FakeClient()
    p = Pairing(c, MY_ID)
    assert p.add_device(PEER_ID.lower().replace("-", ""), "  PC-B  ") == PEER_ID
    sent = c.called("add_device")[0][0]
    assert sent["deviceID"] == PEER_ID and sent["name"] == "PC-B"
    assert sent["addresses"] == ["dynamic"]
    assert sent["introducer"] is False and sent["autoAcceptFolders"] is False and sent["paused"] is False
    assert sent["compression"] == "metadata"  # ひな形の項目が残っている
    assert sent["maxSendKbps"] == 0


def test_add_device_without_name_uses_short_id():
    c = FakeClient()
    Pairing(c, MY_ID).add_device(PEER_ID)
    assert c.devices[PEER_ID]["name"] == PEER_ID.split("-")[0]


def test_add_existing_device_updates_instead_of_duplicating():
    c = FakeClient()
    p = Pairing(c, MY_ID)
    p.add_device(PEER_ID, "old")
    c.devices[PEER_ID]["paused"] = True
    p.add_device(PEER_ID, "new", addresses=["tcp://127.0.0.1:50000"])
    assert len(c.called("add_device")) == 1
    assert c.devices[PEER_ID]["name"] == "new"
    assert c.devices[PEER_ID]["addresses"] == ["tcp://127.0.0.1:50000"]
    assert c.devices[PEER_ID]["paused"] is False


def test_add_self_never_reaches_engine():
    c = FakeClient()
    with pytest.raises(pg.DeviceIdError):
        Pairing(c, MY_ID).add_device(MY_ID)
    assert c.called("add_device") == [] and c.called("get_devices") == []


def test_add_invalid_id_never_reaches_engine():
    c = FakeClient()
    with pytest.raises(pg.DeviceIdError):
        Pairing(c, MY_ID).add_device("not-an-id")
    assert c.calls == []


@pytest.mark.parametrize("exc,kind", [
    (ApiConnectionError("x"), pg.PairingNetworkError),
    (ApiTimeoutError("x"), pg.PairingNetworkError),
    (ApiBadRequestError("x", status=400), pg.PairingRejectedError),
    (ApiAuthError("x", status=403), pg.PairingRejectedError),
])
def test_registration_failures_are_classified(exc, kind):
    c = FakeClient()
    c.fail["add_device"] = exc
    with pytest.raises(kind) as ei:
        Pairing(c, MY_ID).add_device(PEER_ID)
    assert "登録" in str(ei.value)
    assert PEER_ID not in c.devices
    assert ei.value.__cause__ is None


def test_network_down_on_every_operation_is_a_pairing_error():
    c = FakeClient()
    p = Pairing(c, MY_ID)
    for name in ("get_devices", "pending_devices", "pending_folders", "connections", "get_folders"):
        c.fail[name] = ApiConnectionError("down")
    for call in (lambda: p.add_device(PEER_ID), p.pending_devices, p.pending_folders,
                 lambda: p.peer_state(PEER_ID), p.configured_peers):
        with pytest.raises(pg.PairingNetworkError):
            call()


def test_remove_device():
    c = FakeClient()
    p = Pairing(c, MY_ID)
    p.add_device(PEER_ID)
    p.remove_device(PEER_ID)
    assert PEER_ID not in c.devices
    p.remove_device(PEER_ID)  # 既に無くても落ちない
    assert [d["deviceID"] for d in p.configured_peers()] == []


# ---------------------------------------------------------------- 承認待ち
def test_pending_devices_and_accept_reject():
    c = FakeClient()
    c.pending_dev = {
        PEER_ID: {"time": "2026-11-21T10:00:00Z", "name": "PC-B", "address": "192.168.1.2:22000"},
        pg.normalize_device_id("A" * 52): {"time": "2026-11-20T10:00:00Z"},
    }
    p = Pairing(c, MY_ID)
    pend = p.pending_devices()
    assert [d.device_id for d in pend][0] == PEER_ID  # 新しい順
    assert pend[0].name == "PC-B" and pend[0].address == "192.168.1.2:22000"
    assert pend[1].name == ""  # 欠けている項目は空文字（落ちない）
    p.accept_pending_device(PEER_ID, "PC-B")
    assert PEER_ID in c.devices
    p.reject_pending_device("SOMEONE-ELSE")
    assert c.called("dismiss_pending_device") == [("SOMEONE-ELSE",)]


def test_pending_folders():
    c = FakeClient()
    c.pending_fol = {"fl-abcde-fghij": {"offeredBy": {PEER_ID: {"time": "2026-11-21T10:00:00Z", "label": "共有"}}}}
    pend = Pairing(c, MY_ID).pending_folders()
    assert pend == [pg.PendingFolder("fl-abcde-fghij", "共有", PEER_ID, "2026-11-21T10:00:00Z")]


def test_pending_empty():
    p = Pairing(FakeClient(), MY_ID)
    assert p.pending_devices() == [] and p.pending_folders() == []


# ---------------------------------------------------------------- 接続状態
def test_peer_state_variants():
    c = FakeClient()
    p = Pairing(c, MY_ID)
    assert p.peer_state(PEER_ID) == pg.PeerState(configured=False, connected=False, paused=False)
    p.add_device(PEER_ID)
    c.conns["connections"][PEER_ID] = {"connected": False, "paused": False, "address": "", "type": ""}
    s = p.peer_state(PEER_ID)
    assert s.configured and not s.connected and s.address is None and s.via_relay is None
    c.conns["connections"][PEER_ID] = {"connected": True, "paused": False, "address": "1.2.3.4:22000",
                                       "type": "tcp-client", "clientVersion": "v2.1.7"}
    s = p.peer_state(PEER_ID)
    assert s.connected and s.address == "1.2.3.4:22000" and s.via_relay is False and s.client_version == "v2.1.7"
    c.conns["connections"][PEER_ID]["type"] = "relay-client"
    assert p.peer_state(PEER_ID).via_relay is True


# ---------------------------------------------------------------- フォルダ
def test_folder_id_generation_and_validation():
    ids = {pg.generate_folder_id() for _ in range(50)}
    assert len(ids) == 50
    for fid in ids:
        assert fid.startswith("fl-") and len(fid) == 14 and pg.is_valid_folder_id(fid)
    for bad in ("", None, "a b", "x/y", "日本語", "a" * 65):
        assert not pg.is_valid_folder_id(bad)


def test_share_folder_creates_with_versioning_and_both_devices(tmp_path):
    c = FakeClient()
    p = Pairing(c, MY_ID)
    folder = tmp_path / "共有フォルダ"
    folder.mkdir()
    cfg = p.share_folder("fl-abcde-fghij", folder, PEER_ID, label="共有", versioning_days=30)
    assert cfg["id"] == "fl-abcde-fghij" and cfg["path"] == str(folder) and cfg["label"] == "共有"
    assert cfg["type"] == "sendreceive"
    assert [d["deviceID"] for d in cfg["devices"]] == [MY_ID, PEER_ID]
    assert cfg["versioning"]["type"] == "staggered"
    assert cfg["versioning"]["params"] == {"maxAge": str(30 * 86400)}
    assert cfg["versioning"]["fsPath"] == ""  # ひな形の項目は残る
    assert cfg["maxConflicts"] == 10 and cfg["ignoreDelete"] is False and cfg["paused"] is False
    assert cfg["rescanIntervalS"] == 3600 and cfg["markerName"] == ".stfolder"


def test_same_folder_id_with_different_paths_per_pc(tmp_path):
    """共通のフォルダ ID を、PC ごとに違うパスで使える。"""
    a, b = FakeClient(MY_ID), FakeClient(PEER_ID)
    (tmp_path / "a").mkdir()
    (tmp_path / "b" / "deep").mkdir(parents=True)
    Pairing(a, MY_ID).share_folder("fl-abcde-fghij", tmp_path / "a", PEER_ID)
    Pairing(b, PEER_ID).share_folder("fl-abcde-fghij", tmp_path / "b" / "deep", MY_ID)
    assert a.folders["fl-abcde-fghij"]["path"] != b.folders["fl-abcde-fghij"]["path"]
    assert {d["deviceID"] for d in a.folders["fl-abcde-fghij"]["devices"]} == \
           {d["deviceID"] for d in b.folders["fl-abcde-fghij"]["devices"]} == {MY_ID, PEER_ID}


def test_share_existing_folder_adds_peer_without_recreating(tmp_path):
    c = FakeClient()
    p = Pairing(c, MY_ID)
    p.share_folder("fl-abcde-fghij", tmp_path, PEER_ID, paused=True)
    c.folders["fl-abcde-fghij"]["devices"] = [{"deviceID": MY_ID}]
    c.folders["fl-abcde-fghij"]["maxConflicts"] = 0
    cfg = p.share_folder("fl-abcde-fghij", tmp_path, PEER_ID)
    assert len(c.called("add_folder")) == 1
    assert [d["deviceID"] for d in cfg["devices"]] == [MY_ID, PEER_ID]
    assert cfg["paused"] is False
    assert cfg["maxConflicts"] == 10  # 競合コピーを作らない設定は直す


def test_refuses_to_repoint_folder_id_to_another_path(tmp_path):
    c = FakeClient()
    p = Pairing(c, MY_ID)
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    p.share_folder("fl-abcde-fghij", tmp_path / "one", PEER_ID)
    with pytest.raises(pg.FolderConflictError):
        p.share_folder("fl-abcde-fghij", tmp_path / "two", PEER_ID)
    assert c.folders["fl-abcde-fghij"]["path"] == str(tmp_path / "one")
    assert c.called("patch_folder") == []


def test_refuses_same_path_under_two_folder_ids(tmp_path):
    c = FakeClient()
    p = Pairing(c, MY_ID)
    p.share_folder("fl-abcde-fghij", tmp_path, PEER_ID)
    with pytest.raises(pg.FolderConflictError):
        p.share_folder("fl-zzzzz-zzzzz", tmp_path, PEER_ID)


def test_share_folder_rejects_bad_input(tmp_path):
    c = FakeClient()
    p = Pairing(c, MY_ID)
    with pytest.raises(pg.PairingError):
        p.share_folder("bad id", tmp_path, PEER_ID)
    with pytest.raises(pg.DeviceIdError):
        p.share_folder("fl-abcde-fghij", tmp_path, MY_ID)
    with pytest.raises(pg.FolderPathError):
        p.share_folder("fl-abcde-fghij", tmp_path / "missing", PEER_ID)
    assert c.called("add_folder") == []


def test_share_folder_failures(tmp_path):
    c = FakeClient()
    c.fail["add_folder"] = ApiConnectionError("down")
    with pytest.raises(pg.PairingNetworkError):
        Pairing(c, MY_ID).share_folder("fl-abcde-fghij", tmp_path, PEER_ID)
    c.fail["add_folder"] = ApiBadRequestError("folder path empty", status=400)
    with pytest.raises(pg.PairingRejectedError):
        Pairing(c, MY_ID).share_folder("fl-abcde-fghij", tmp_path, PEER_ID)


def test_bad_versioning_days_fall_back_to_30(tmp_path):
    c = FakeClient()
    cfg = Pairing(c, MY_ID).share_folder("fl-abcde-fghij", tmp_path, PEER_ID, versioning_days=0)
    assert cfg["versioning"]["params"]["maxAge"] == str(30 * 86400)


def test_unshare_does_not_touch_files(tmp_path):
    c = FakeClient()
    p = Pairing(c, MY_ID)
    (tmp_path / "keep.txt").write_text("data", encoding="utf-8")
    p.share_folder("fl-abcde-fghij", tmp_path, PEER_ID)
    p.unshare_folder("fl-abcde-fghij")
    p.unshare_folder("fl-abcde-fghij")  # 2回目も落ちない
    assert "fl-abcde-fghij" not in c.folders
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "data"


def test_set_folder_paused(tmp_path):
    c = FakeClient()
    p = Pairing(c, MY_ID)
    p.share_folder("fl-abcde-fghij", tmp_path, PEER_ID)
    p.set_folder_paused("fl-abcde-fghij", True)
    assert c.folders["fl-abcde-fghij"]["paused"] is True


# ---------------------------------------------------------------- 場所の検証
def test_validate_sync_folder_rejections(tmp_path, monkeypatch):
    with pytest.raises(pg.FolderPathError):
        pg.validate_sync_folder("")
    with pytest.raises(pg.FolderPathError):
        pg.validate_sync_folder("relative/path")
    drive_root = Path(os.path.abspath(os.sep))
    with pytest.raises(pg.FolderPathError):
        pg.validate_sync_folder(drive_root)
    with pytest.raises(pg.FolderPathError):
        pg.validate_sync_folder(Path.home())
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(pg.FolderPathError):
        pg.validate_sync_folder(f)
    win = tmp_path / "FakeWindows"
    (win / "System32").mkdir(parents=True)
    monkeypatch.setenv("WINDIR", str(win))
    with pytest.raises(pg.FolderPathError):
        pg.validate_sync_folder(win / "System32")
    # FolderLink 自身の設定フォルダ、その中、それを含む親はどれも不可
    data = tmp_path / "AppData" / "FolderLink"
    (data / "syncthing").mkdir(parents=True)
    for bad in (data, data / "syncthing", tmp_path / "AppData"):
        with pytest.raises(pg.FolderPathError):
            pg.validate_sync_folder(bad, forbidden=[data])
    ok = tmp_path / "ok"
    ok.mkdir()
    assert pg.validate_sync_folder(ok, forbidden=[data]) == ok


def test_scan_folder_contents(tmp_path):
    assert pg.scan_folder_contents(tmp_path).is_empty
    (tmp_path / "a.txt").write_bytes(b"12345")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"x" * 100)
    (tmp_path / ".stfolder").mkdir()
    (tmp_path / ".stversions").mkdir()
    (tmp_path / ".stversions" / "old.txt").write_bytes(b"zzz")
    (tmp_path / ".stignore").write_text("x", encoding="utf-8")
    c = pg.scan_folder_contents(tmp_path)
    assert (c.files, c.dirs, c.bytes, c.truncated) == (2, 1, 105, False)
    assert not c.is_empty
    assert pg.scan_folder_contents(tmp_path, limit=1).truncated is True
