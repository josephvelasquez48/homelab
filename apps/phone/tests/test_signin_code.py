"""Sign-in codes: a signed-in page hands out a 6-digit code that signs one
other device in (the Quest's call panel), once, for 2 minutes."""

import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture
def make_client(monkeypatch):
    monkeypatch.setattr(main, "PASSWORD", "correct horse")
    monkeypatch.setattr(main, "_signin_code", None)

    async def no_hub():
        pass

    async def no_sleep(_):
        pass

    monkeypatch.setattr(main.hub, "run", no_hub)
    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)  # the anti-guessing delay
    clients = []

    def make():
        c = TestClient(main.app, base_url="https://phone.home:8443")
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)


def signed_in(make_client):
    c = make_client()
    c.post("/login", data={"password": "correct horse"})
    return c


def is_signed_in(client) -> bool:
    return 'id="call-card"' in client.get("/").text


def test_code_needs_a_signed_in_page(make_client):
    assert make_client().post("/api/signin-code").status_code == 401


def test_code_signs_in_another_device_once(make_client):
    code = signed_in(make_client).post("/api/signin-code").json()["code"]
    assert len(code) == 6 and code.isdigit()

    quest = make_client()
    r = quest.post("/login/code", data={"code": code}, follow_redirects=False)
    assert r.headers["location"] == "/"
    assert is_signed_in(quest)

    again = make_client()
    r = again.post("/login/code", data={"code": code}, follow_redirects=False)
    assert r.headers["location"] == "/?failed=code"
    assert not is_signed_in(again)


def test_wrong_code_fails(make_client):
    signed_in(make_client).post("/api/signin-code")
    quest = make_client()
    r = quest.post("/login/code", data={"code": "000000x"}, follow_redirects=False)
    assert r.headers["location"] == "/?failed=code"
    assert not is_signed_in(quest)


def test_no_code_issued_means_no_login(make_client):
    quest = make_client()
    r = quest.post("/login/code", data={"code": ""}, follow_redirects=False)
    assert r.headers["location"] == "/?failed=code"


def test_expired_code_fails(make_client, monkeypatch):
    code = signed_in(make_client).post("/api/signin-code").json()["code"]
    later = main.time.monotonic() + main.SIGNIN_CODE_TTL + 1
    monkeypatch.setattr(main.time, "monotonic", lambda: later)
    r = make_client().post("/login/code", data={"code": code}, follow_redirects=False)
    assert r.headers["location"] == "/?failed=code"


def test_too_many_wrong_guesses_burn_the_code(make_client):
    code = signed_in(make_client).post("/api/signin-code").json()["code"]
    wrong = "111111" if code != "111111" else "222222"
    guesser = make_client()
    for _ in range(main.SIGNIN_CODE_MAX_FAILURES):
        guesser.post("/login/code", data={"code": wrong})
    r = make_client().post("/login/code", data={"code": code}, follow_redirects=False)
    assert r.headers["location"] == "/?failed=code"


def test_new_code_replaces_the_old_one(make_client):
    page = signed_in(make_client)
    first = page.post("/api/signin-code").json()["code"]
    second = page.post("/api/signin-code").json()["code"]
    if first == second:
        pytest.skip("random codes collided")
    r = make_client().post("/login/code", data={"code": first}, follow_redirects=False)
    assert r.headers["location"] == "/?failed=code"
    r = make_client().post("/login/code", data={"code": second}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_login_page_offers_the_code_form(make_client):
    assert 'action="/login/code"' in make_client().get("/").text
