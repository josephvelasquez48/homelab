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


def test_a_link_works_until_turned_off(links, tmp_path):
    link = links.create("Sam")
    link.created -= 365 * 24 * 3600  # a year on: still works
    assert links.live(link.token) and Links(tmp_path / "send-links.json").live(link.token)  # kept on disk
    links.revoke(link.token)
    assert links.live(link.token) is None and links.active() == []


def test_links_made_with_an_expiry_still_load_and_now_last(tmp_path):
    path = tmp_path / "send-links.json"
    path.write_text('[{"token": "t", "created": 1, "expires": 2, "label": "Sam", "revoked": false, "sends": 3, "bytes": 9, "last_send": 0}]')
    links = Links(path)
    assert links.live("t") and links.get("t").sends == 3


def test_link_caps_are_per_day(links):
    link = links.create("Sam")
    links.check_send(link, 1000, now=1000.0)
    links.record_send(link, 1000, now=1000.0)
    with pytest.raises(LinkError, match="Slow down"):
        links.check_send(link, 1, now=1001.0)  # straight after the last
    with pytest.raises(LinkError, match="more than"):
        links.check_send(link, guests_module.MAX_BYTES, now=1010.0)
    link.day_sends = guests_module.MAX_SENDS
    with pytest.raises(LinkError, match="as much as"):
        links.check_send(link, 1, now=1010.0)
    later = 1000.0 + guests_module.DAY_SECONDS  # the next day: open again
    links.check_send(link, 1, now=later)
    links.record_send(link, 1, now=later)
    assert (link.day_sends, link.day_bytes, link.sends) == (1, 1, 2)
    links.revoke(link.token)
    with pytest.raises(LinkError, match="turned off"):
        links.check_send(link, 1, now=later + 10)


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
    link = links.create("Sam")
    assert app_client.get(f"/send/{link.token}").status_code == 404  # only on the guest listener


# ---- the guest's side -------------------------------------------------------------


def test_guest_page_for_a_live_link_and_a_dead_one(guest, links):
    link = links.create("Sam")
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
    link = links.create("Sam")
    links.revoke(link.token)
    assert guest.post(f"/send/{link.token}", data={"text": "hi"}).status_code == 404
    monkeypatch.setitem(main.hub.settings, "linkMbPerSend", 1)
    r = guest.post(f"/send/{links.create("Sam").token}", files=[("files", ("big.bin", b"x" * (1024 * 1024 + 1), "application/octet-stream"))])
    assert r.status_code == 413


def test_guest_off_the_home_network_is_refused(monkeypatch, links):
    monkeypatch.setattr(main.hub, "links", links)
    client = TestClient(main.guest_app)  # "testclient" isn't on 192.168.1.0/24
    link = links.create("Sam")
    assert client.get(f"/send/{link.token}").status_code == 403
    assert client.post(f"/send/{link.token}", data={"text": "hi"}).status_code == 403


def test_guest_listener_serves_nothing_else(guest):
    for path in ("/", "/api/drops", "/static/app.js", "/login", "/ws", "/api/agent/ringing"):
        assert guest.get(path).status_code == 404, path


# ---- Settings: the limits --------------------------------------------------------


@pytest.mark.asyncio
async def test_limits_are_set_in_range_and_applied(tmp_path):
    from app.hub import Hub

    hub = Hub(telephony=object(), bridge=object(), settings_path=tmp_path / "s.json")
    hub.drops, hub.links = Drops(tmp_path / "drops"), Links(tmp_path / "links.json")
    hub.apply_limits()

    async def nothing(*a, **k):
        pass

    hub.broadcast_state = nothing
    hub.tick = nothing
    page = object()
    assert await hub.command(page, {"action": "set-limit", "key": "linkSendsPerDay", "value": 5}) is None
    assert hub.links.max_sends == 5
    await hub.command(page, {"action": "set-limit", "key": "dropMaxFileMb", "value": 999999})
    assert hub.settings["dropMaxFileMb"] == 500 and hub.drops.max_file == 500 * 1024 * 1024  # held to its top
    await hub.command(page, {"action": "set-limit", "key": "dropKeepDays", "value": 0})
    assert hub.settings["dropKeepDays"] == 1 and hub.drops.keep_seconds == 24 * 3600
    assert await hub.command(page, {"action": "set-limit", "key": "micLabel", "value": 1}) == "unknown setting"
    assert await hub.command(page, {"action": "set-limit", "key": "linkMbPerDay", "value": "lots"}) == "Not a number"
    # Saved, and a reload keeps them.
    assert Hub(telephony=object(), bridge=object(), settings_path=tmp_path / "s.json").settings["linkSendsPerDay"] == 5


def test_a_hand_edited_settings_file_is_held_to_the_limits(tmp_path):
    from app.hub import load_settings

    path = tmp_path / "s.json"
    path.write_text('{"linkMbPerDay": 1, "dropKeepDays": 1000}')
    s = load_settings(path)
    assert (s["linkMbPerDay"], s["dropKeepDays"]) == (10, 90)
