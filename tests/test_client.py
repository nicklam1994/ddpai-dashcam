"""離線測試：用假相機驗證策略矩陣、解析、下載、Web API。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_camera import FILES, MEDIA, FakeCamera  # noqa: E402

from ddpai_mini3s import Camera, discovery  # noqa: E402
from ddpai_mini3s.parser import find_list, maybe_json, unwrap  # noqa: E402
from ddpai_mini3s.transport import STRATEGY_BY_ID, Transport  # noqa: E402


@pytest.fixture(params=["plain", "doilel", "session"])
def camera(request):
    with FakeCamera(mode=request.param) as fake:
        cam = Camera(host=fake.host, timeout=3.0, auto_discover=False)
        yield cam, fake
        cam.close()


# ── 傳輸層 ─────────────────────────────────────────────────────
def test_detect_each_firmware_mode(camera):
    """三種韌體模式都必須自動探測到可用策略。"""
    cam, _ = camera
    strategy = cam.connect()
    assert strategy.id in {
        "vcam-get-plain", "vcam-get-doilel", "vcam-get-session",
        "vcam-get-doilel-session", "vcam-post-session", "vcam-post-plain",
    }


def test_doilel_mode_requires_header():
    """doilel 模式：沒帶 header 必須 403，帶了才行。"""
    with FakeCamera(mode="doilel") as fake:
        plain = Transport(host=fake.host, timeout=2.0)
        rejected = plain._send(STRATEGY_BY_ID["vcam-get-plain"], "API_GetBaseInfo")
        assert rejected.ok is False and rejected.status == 403
        accepted = plain._send(STRATEGY_BY_ID["vcam-get-doilel"], "API_GetBaseInfo")
        assert accepted.ok is True
        plain.close()

        cam = Camera(host=fake.host, timeout=2.0, auto_discover=False)
        assert cam.connect().doi_lel is True
        cam.close()


def test_session_token_acquired():
    with FakeCamera(mode="session") as fake:
        cam = Camera(host=fake.host, timeout=2.0, auto_discover=False)
        cam.connect()
        assert cam.transport.session_token == "TESTTOKEN1234"
        cam.close()


def test_htmlwall_raises():
    with FakeCamera(mode="htmlwall") as fake:
        cam = Camera(host=fake.host, timeout=1.0, auto_discover=False)
        with pytest.raises(Exception):
            cam.connect()
        cam.close()


# ── 解析 ───────────────────────────────────────────────────────
def test_unwrap_nested_json_string():
    assert unwrap({"errcode": 0, "data": '{"sdcapacity": "10"}'}) == {"sdcapacity": "10"}


def test_find_list_handles_variants():
    assert len(find_list({"file": [{"name": "a"}]})) == 1
    assert len(find_list({"data": {"file": [{"name": "a"}, {"name": "b"}]}})) == 2
    assert find_list("not json") == []
    assert maybe_json("[1,2]") == [1, 2]


# ── 高階 client ────────────────────────────────────────────────
def test_info_and_storage(camera):
    cam, _ = camera
    info = cam.info()
    assert info.model == "mini3s"
    assert info.sn == "MINI3S000123"

    storage = cam.storage()
    assert storage is not None
    assert storage.total_bytes == 64 * 1024 * 1024 * 1024
    assert storage.free_bytes == 32 * 1024 * 1024 * 1024
    assert storage.used_percent == 50.0


def test_files_and_events(camera):
    cam, _ = camera
    files = cam.files()
    assert len(files) == 3
    assert files[0].name.endswith(".MP4")
    assert files[0].basename == "FILE_20260101120000_0001.MP4"
    assert files[0].duration == 60
    assert files[0].start is not None

    events = cam.events()
    assert len(events) == 1
    assert events[0].kind == "event"
    assert events[0].size == 20971520


def test_download_and_skip(camera, tmp_path):
    cam, _ = camera
    name = FILES[0]["name"]
    path = cam.download(name, tmp_path)
    assert path.exists()
    assert path.read_bytes() == MEDIA[name]

    # 第二次應該跳過（遠端 HEAD 大小相同）
    before = path.stat().st_mtime_ns
    again = cam.download(name, tmp_path)
    assert again == path and again.stat().st_mtime_ns == before


def test_download_many_toggles_super_download(camera, tmp_path):
    cam, fake = camera
    results = cam.download_many([FILES[0]["name"], FILES[1]["name"]], dest_dir=tmp_path)
    assert all(not str(v).startswith("ERROR") for v in results.values())
    cmds = [params.get("cmd") for _, _, params, _ in fake.requests]
    assert "API_SuperDownload" in cmds
    assert (tmp_path / "FILE_20260101120100_0002.MP4").exists()


def test_rtsp_url(camera):
    cam, fake = camera
    assert cam.rtsp_url() == f"rtsp://127.0.0.1:554/liveRTSP/av4"


def test_doctor_report(camera):
    cam, _ = camera
    report = cam.doctor()
    assert report["tcp"] is True
    assert report["strategy"]
    assert report["commands"]["API_GetBaseInfo"]["ok"] is True


def test_photo_capability_reports_unsupported(camera):
    """假相機模擬「沒有照片 API」的韌體（與真實 Mini3 Pro 一致）。"""
    cam, _ = camera
    cap = cam.photo_support()
    assert cap["supported"] is False
    assert "照片" in cap["reason"]
    # 第二次呼叫要用快取，不再打相機
    assert cam.photo_support() is cap


def test_camera_album_bundles_events_and_videos(camera):
    cam, _ = camera
    album = cam.camera_album()
    assert album["photos"]["supported"] is False
    assert album["events"]["count"] == 1
    assert album["videos"]["count"] == 3
    assert album["videos"]["items"][0]["basename"].endswith(".MP4")


# ── 發現 ───────────────────────────────────────────────────────
def test_discovery_finds_fake_camera():
    with FakeCamera(mode="plain") as fake:
        host = discovery.discover(hosts=[fake.host], timeout=2.0)
        assert host == fake.host


def test_discovery_returns_none_when_nothing_there():
    assert discovery.discover(hosts=["127.0.0.1:1"], timeout=0.5) is None


# ── Web ────────────────────────────────────────────────────────
@pytest.fixture
def web_client(monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    with FakeCamera(mode="plain") as fake:
        monkeypatch.setenv("DDPAI_HOST", fake.host)
        import importlib

        from ddpai_mini3s.web import app as web_app

        importlib.reload(web_app)
        with fastapi_testclient.TestClient(web_app.app) as client:
            yield client, fake


def test_web_health_and_files(web_client):
    client, _ = web_client
    health = client.get("/api/health").json()
    assert health["ok"] is True and health["data"]["model"] == "mini3s"

    files = client.get("/api/files").json()
    assert files["ok"] is True and len(files["data"]) == 3


def test_web_index_served(web_client):
    client, _ = web_client
    page = client.get("/")
    assert page.status_code == 200
    assert "DDPAI" in page.text


def test_web_media_range(web_client):
    client, _ = web_client
    name = FILES[0]["name"].lstrip("/")
    full = client.get(f"/api/media/{name}")
    assert full.status_code == 200
    part = client.get(f"/api/media/{name}", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206
    assert len(part.content) == 10


def test_web_download(web_client, tmp_path, monkeypatch):
    client, _ = web_client
    from ddpai_mini3s.web import app as web_app

    monkeypatch.setattr(web_app, "DOWNLOAD_DIR", tmp_path)
    result = client.post("/api/download", json={"names": [FILES[2]["name"]]}).json()
    assert result["ok"] is True
    assert (tmp_path / "FILE_20260101120200_0003.MP4").exists()
