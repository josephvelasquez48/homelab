import time

import pytest
from fastapi.testclient import TestClient

import app.drops as drops_module
import app.main as main
from app.drops import DropError, Drops, safe_name

AGENT = {"Authorization": "Bearer agent-token"}
SHORTCUT = {"Authorization": "Bearer drop-token"}


@pytest.fixture
def drops(tmp_path):
    return Drops(tmp_path / "drops")


@pytest.fixture
def client(monkeypatch, drops):
    monkeypatch.setattr(main, "PASSWORD", "correct horse")
    monkeypatch.setattr(main, "AGENT_TOKEN", "agent-token")
    monkeypatch.setattr(main, "DROP_TOKEN", "drop-token")
    monkeypatch.setattr(main.hub, "drops", drops)

    async def no_hub():
        pass

    monkeypatch.setattr(main.hub, "run", no_hub)
    with TestClient(main.app, base_url="https://phone.home:8443") as c:
        yield c


def login(client):
    client.post("/login", data={"password": "correct horse"})


# ---- the store ------------------------------------------------------------------


def test_text_and_files_are_kept_and_survive_a_restart(drops, tmp_path):
    drops.add_text("hello", "phone")
    f = drops.add_file("IMG_0001.jpg", "image/jpeg", b"jpeg-bytes", "phone")
    again = Drops(tmp_path / "drops")
    assert [d["kind"] for d in again.recent()] == ["file", "text"]  # newest first
    assert again.file_path(again.get(f.id)).read_bytes() == b"jpeg-bytes"


def test_limits(drops, monkeypatch):
    with pytest.raises(DropError):
        drops.add_text("   ", "pc")
    with pytest.raises(DropError):
        drops.add_text("x" * (drops_module.MAX_TEXT + 1), "pc")
    with pytest.raises(DropError):
        drops.add_file("empty.txt", None, b"", "pc")
    monkeypatch.setattr(drops_module, "MAX_FILE", 4)
    with pytest.raises(DropError):
        drops.add_file("big.bin", None, b"12345", "pc")
    with pytest.raises(DropError):
        drops.add_text("hi", "somewhere")


def test_old_and_extra_drops_are_pruned_with_their_files(drops, monkeypatch):
    old = drops.add_file("old.png", None, b"x", "phone")
    old.created -= drops_module.KEEP_SECONDS + 1
    drops.prune()
    assert drops.get(old.id) is None and not drops.file_path(old).exists()
    monkeypatch.setattr(drops_module, "KEEP_COUNT", 3)
    for i in range(5):
        drops.add_text(f"t{i}", "pc")
    assert [d["text"] for d in drops.recent()] == ["t4", "t3", "t2"]


def test_file_names_are_made_safe():
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("C:\\Users\\x\\a:b?.txt") == "a_b_.txt"
    assert safe_name("...") == "file"


def test_the_pc_gets_only_new_drops_from_the_phone(drops):
    first = drops.add_text("one", "phone")
    drops.add_text("from the pc", "pc")
    time.sleep(0.01)
    second = drops.add_text("two", "phone")
    assert [d["text"] for d in drops.for_pc_since(0)] == ["one", "two"]
    assert [d["id"] for d in drops.for_pc_since(first.created)] == [second.id]


# ---- the API ----------------------------------------------------------------------


def test_page_drops_need_a_login(client):
    assert client.post("/api/drops", data={"text": "hi"}).status_code == 401
    assert client.get("/api/drops/abc/file").status_code == 401
    login(client)
    r = client.post("/api/drops", data={"text": "hi", "source": "phone"})
    assert r.status_code == 200 and r.json()["made"][0]["text"] == "hi"


def test_page_upload_text_and_photos_then_save_and_delete(client, drops):
    login(client)
    r = client.post("/api/drops", data={"text": "caption", "source": "pc"},
                    files=[("files", ("a.png", b"png-bytes", "image/png")), ("files", ("b.pdf", b"pdf", "application/pdf"))])
    made = r.json()["made"]
    assert [d["kind"] for d in made] == ["file", "file", "text"]
    photo = made[0]
    r = client.get(f"/api/drops/{photo['id']}/file")
    assert r.content == b"png-bytes" and r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"].startswith("inline")
    assert client.get(f"/api/drops/{photo['id']}/file?download=1").headers["content-disposition"].startswith("attachment")
    assert client.delete(f"/api/drops/{photo['id']}").status_code == 200
    assert client.get(f"/api/drops/{photo['id']}/file").status_code == 404


def test_an_empty_drop_is_refused(client):
    login(client)
    r = client.post("/api/drops", data={"text": "  "})
    assert r.status_code == 400 and r.json()["detail"] == "Nothing to send"


def test_shortcut_token_only_makes_drops_from_the_phone(client, drops):
    assert client.post("/api/shortcut/drop", data={"text": "hi"}).status_code == 401
    assert client.post("/api/shortcut/drop", data={"text": "hi"}, headers=AGENT).status_code == 401
    r = client.post("/api/shortcut/drop", files=[("files", ("IMG.heic", b"heic", "image/heic"))], headers=SHORTCUT)
    assert r.status_code == 200 and drops.recent()[0]["source"] == "phone"
    # The drop token opens nothing else.
    assert client.get("/api/agent/drops", headers=SHORTCUT).status_code == 401
    assert client.get("/api/agent/ringing", headers=SHORTCUT).status_code == 401
    assert client.get(f"/api/drops/{drops.recent()[0]['id']}/file", headers=SHORTCUT).status_code == 401


def test_no_drop_token_configured_means_no_shortcut(client, monkeypatch):
    monkeypatch.setattr(main, "DROP_TOKEN", "")
    assert client.post("/api/shortcut/drop", data={"text": "hi"}, headers={"Authorization": "Bearer "}).status_code == 401


def test_agent_sees_new_drops_from_the_phone_and_fetches_them(client, drops):
    status = client.get("/api/agent/ringing", headers=AGENT).json()
    assert status["dropLatest"] == 0
    d = drops.add_file("photo.jpg", "image/jpeg", b"jpg", "phone")
    assert client.get("/api/agent/ringing", headers=AGENT).json()["dropLatest"] == d.created
    listed = client.get("/api/agent/drops?since=0", headers=AGENT).json()["drops"]
    assert [x["id"] for x in listed] == [d.id]
    assert client.get(f"/api/agent/drops/{d.id}/file", headers=AGENT).content == b"jpg"
    assert client.get("/api/agent/drops?since=0").status_code == 401


def test_home_screen_files_are_served_without_a_login(client):
    r = client.get("/static/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    assert r.json()["display"] == "standalone"
    assert client.get("/static/icon-180.png").content.startswith(b"\x89PNG")
    assert 'rel="apple-touch-icon"' in client.get("/").text  # on the login page too


def test_shortcut_text_arrives_as_text(client, drops):
    r = client.post("/api/shortcut/drop", files=[("files", ("text.txt", "héllo".encode(), "text/plain"))], headers=SHORTCUT)
    assert r.status_code == 200 and drops.recent()[0]["kind"] == "text" and drops.recent()[0]["text"] == "héllo"
