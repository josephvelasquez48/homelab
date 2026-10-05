from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.guests as guests_module
import app.main as main
from app.drops import Drops
from app.guests import LinkError, Links


@pytest.fixture
def links(tmp_path):
    return Links(tmp_path / "send-links.json")


@pytest.fixture
def drops(tmp_path):
    return Drops(tmp_path / "drops")


@pytest.fixture
def app_client(monkeypatch, links, drops):
    monkeypatch.setattr(main, "PASSWORD", "correct horse")
    monkeypatch.setattr(main.hub, "drops", drops)
    monkeypatch.setattr(main.hub, "links", links)
    monkeypatch.setattr(main.hub, "links_base", "http://192.168.1.253:8081")

    async def no_hub():
        pass

    monkeypatch.setattr(main.hub, "run", no_hub)
    monkeypatch.setattr(main, "GUEST_PORT", 0)  # no real guest listener in tests
    with TestClient(main.app, base_url="https://phone.home:8443") as c:
        c.post("/login", data={"password": "correct horse"})
        yield c


@pytest.fixture
def guest(monkeypatch, links, drops):
    """The guest listener, as a guest on the home Wi-Fi."""
    monkeypatch.setattr(main.hub, "drops", drops)
    monkeypatch.setattr(main.hub, "links", links)
    monkeypatch.setattr(main, "on_home_network", lambda client: True)
    return TestClient(main.guest_app, base_url="http://192.168.1.253:8081")


# ---- links ----------------------------------------------------------------------


def test_a_link_lasts_a_day_and_can_be_turned_off(links, tmp_path):
    link = links.create("Sam")
    assert 24 * 3600 - 5 < link.expires - link.created <= 24 * 3600
    assert links.live(link.token) and Links(tmp_path / "send-links.json").live(link.token)  # kept on disk
    links.revoke(link.token)
    assert links.live(link.token) is None and links.active() == []


def test_link_caps(links, monkeypatch):
    link = links.create()
    links.check_send(link, 1000)
    links.record_send(link, 1000)
    with pytest.raises(LinkError, match="Slow down"):
        links.check_send(link, 1)  # straight after the last
    with pytest.raises(LinkError, match="more than"):
        links.check_send(link, guests_module.MAX_BYTES, now=link.last_send + 10)
    link.sends = guests_module.MAX_SENDS
    with pytest.raises(LinkError, match="used as much"):
        links.check_send(link, 1, now=link.last_send + 10)
    link.expires = 0
    with pytest.raises(LinkError, match="expired"):
        links.check_send(link, 1)


def test_only_the_home_network_counts():
    lan, other, local = (SimpleNamespace(host=h) for h in ("192.168.1.40", "100.109.104.67", "testclient"))
    assert main.on_home_network(lan)
    assert not main.on_home_network(other)  # a tailnet address, say
    assert not main.on_home_network(local) and not main.on_home_network(None)


# ---- your side: making links -------------------------------------------------------


def test_making_and_turning_off_a_link_needs_a_login(app_client, links):
    r = app_client.post("/api/links", data={"label": "Sam"})
    assert r.status_code == 200 and r.json()["url"].startswith("http://192.168.1.253:8081/send/")
    token = links.active()[0]["token"]
    assert app_client.delete(f"/api/links/{token}").status_code == 200
    assert links.active() == []
    app_client.post("/logout")
    assert app_client.post("/api/links", data={"label": "x"}).status_code == 401


def test_the_main_app_has_no_guest_pages(app_client, links):
    link = links.create()
    assert app_client.get(f"/send/{link.token}").status_code == 404  # only on the guest listener


# ---- the guest's side -------------------------------------------------------------


def test_guest_page_for_a_live_link_and_a_dead_one(guest, links):
    link = links.create()
    r = guest.get(f"/send/{link.token}")
    assert r.status_code == 200 and "Send to" in r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert guest.get("/send/not-a-real-link").status_code == 404
    links.revoke(link.token)
    assert guest.get(f"/send/{link.token}").status_code == 404


def test_guest_sends_text_and_photos_marked_as_theirs(guest, links, drops):
    link = links.create("Sam Lee")
    r = guest.post(f"/send/{link.token}", data={"text": "hello"},
                   files=[("files", ("a.jpg", b"jpg", "image/jpeg"))])
    assert r.status_code == 200 and r.json()["sent"] == 2
    made = drops.recent()
    assert {d["source"] for d in made} == {"guest"} and {d["sender"] for d in made} == {"Sam Lee"}
    assert links.get(link.token).sends == 1
    # They reach the PC's agent like the phone's do.
    assert len(drops.for_pc_since(0)) == 2


def test_who_sent_it_is_the_links_name(guest, links, drops):
    sam = links.create("Sam")
    guest.post(f"/send/{sam.token}", data={"name": "Someone Else", "text": "hi"})  # a name field is ignored
    assert (drops.recent()[0]["sender"], drops.recent()[0]["via"]) == ("Sam", "Sam")


def test_guest_page_asks_for_no_name(guest, links):
    assert 'name="name"' not in guest.get(f"/send/{links.create('Sam').token}").text


def test_a_link_needs_a_name(app_client, links):
    r = app_client.post("/api/links", data={"label": "  "})
    assert r.status_code == 400 and links.active() == []


def test_guest_cant_use_a_dead_link_or_send_too_much(guest, links, monkeypatch):
    link = links.create()
    links.revoke(link.token)
    assert guest.post(f"/send/{link.token}", data={"text": "hi"}).status_code == 404
    monkeypatch.setattr(main, "MAX_GUEST_REQUEST", 10)
    r = guest.post(f"/send/{links.create().token}", data={"text": "this is more than ten bytes"})
    assert r.status_code == 413


def test_guest_off_the_home_network_is_refused(monkeypatch, links):
    monkeypatch.setattr(main.hub, "links", links)
    client = TestClient(main.guest_app)  # "testclient" isn't on 192.168.1.0/24
    link = links.create()
    assert client.get(f"/send/{link.token}").status_code == 403
    assert client.post(f"/send/{link.token}", data={"text": "hi"}).status_code == 403


def test_guest_listener_serves_nothing_else(guest):
    for path in ("/", "/api/drops", "/static/app.js", "/login", "/ws", "/api/agent/ringing"):
        assert guest.get(path).status_code == 404, path
