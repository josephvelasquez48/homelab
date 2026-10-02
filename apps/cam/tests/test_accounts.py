import asyncio
import json

from app import config, main
from tests.conftest import accept, new_authenticator, sign_in


def test_pages_need_a_session(client):
    for path in ["/", "/admin", "/account"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/me").status_code == 401


def test_invite_makes_the_account_with_a_passkey(app, client, make_user):
    user, _ = make_user("alice")
    assert user["username"] == "alice" and not user["is_admin"]
    assert client.get("/api/me").json()["user"]["username"] == "alice"
    assert client.get("/", follow_redirects=False).status_code == 200
    (passkey,) = asyncio.run(app.state.store.passkeys_for(user["id"]))
    assert passkey.backed_up  # the soft authenticator says "synced", like iCloud Keychain


def test_invite_link_works_once(app, client):
    _, token = asyncio.run(app.state.store.create_invite("bob", False, None, 24))
    assert client.get(f"/api/invite/{token}").json() == {"username": "bob", "reset": False}
    assert accept(client, token, new_authenticator()).status_code == 200
    assert client.post(f"/api/invite/{token}/options").status_code == 404
    assert client.get(f"/api/invite/{token}").status_code == 404


def test_invites_expire_after_a_day(app, client):
    invite, _ = asyncio.run(app.state.store.create_invite("bob", False, None, config.INVITE_HOURS))
    lifetime = invite.expires_at - invite.created_at
    assert config.INVITE_HOURS == 24 and round(lifetime.total_seconds()) == 24 * 3600


def test_passkey_must_be_user_verified(app, client):
    """A tap alone isn't enough - it needs Face ID, a fingerprint or the PIN."""
    _, token = asyncio.run(app.state.store.create_invite("bob", False, None, 24))
    options = client.post(f"/api/invite/{token}/options").json()
    r = client.post(f"/api/invite/{token}", json=new_authenticator().create(options, user_verified=False))
    assert r.status_code == 400
    assert client.get(f"/api/invite/{token}").status_code == 200  # the link survives


def test_passkey_for_another_site_is_refused(app, client):
    _, token = asyncio.run(app.state.store.create_invite("bob", False, None, 24))
    options = client.post(f"/api/invite/{token}/options").json()
    phish = new_authenticator()
    phish.origin = "https://cam-login.example.evil"
    assert client.post(f"/api/invite/{token}", json=phish.create(options)).status_code == 400


def test_sign_in_and_out(client, make_user):
    _, authenticator = make_user("alice", sign_in=False)
    assert client.get("/api/me").status_code == 401
    assert sign_in(client, authenticator).status_code == 200
    assert client.get("/api/me").json()["user"]["username"] == "alice"
    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401


def test_unknown_passkey_is_refused(client, make_user):
    make_user("alice", sign_in=False)
    stranger = new_authenticator()
    stranger.user_handle = b"x" * 32
    assert sign_in(client, stranger).status_code == 401


def test_forged_signature_is_refused(client, make_user):
    _, authenticator = make_user("alice", sign_in=False)
    options = client.post("/api/login/options").json()
    assertion = authenticator.get(options)
    assertion["response"]["signature"] = new_authenticator().get(options)["response"]["signature"]
    assert client.post("/api/login", json=assertion).status_code == 401


def test_an_assertion_cant_be_replayed(client, make_user):
    _, authenticator = make_user("alice", sign_in=False)
    options = client.post("/api/login/options").json()
    assertion = authenticator.get(options)
    assert client.post("/api/login", json=assertion).status_code == 200
    client.cookies.clear()
    # The challenge was used up: same answer, no ceremony left to match it.
    assert client.post("/api/login", json=assertion).status_code == 400


def test_turned_off_account_cant_sign_in(app, client, make_user):
    user, authenticator = make_user("alice", sign_in=False)
    asyncio.run(app.state.store.set_disabled(user["id"], True))
    assert sign_in(client, authenticator).status_code == 401


def test_sign_in_is_throttled_per_address(client, make_user):
    _, authenticator = make_user("alice", sign_in=False)
    stranger = new_authenticator()
    for _ in range(main.MAX_FAILURES):
        sign_in(client, stranger)
    assert client.post("/api/login/options").status_code == 429


def test_failures_dont_lock_anyone_else_out(client, make_user):
    """The password version locked a *name* after five failures, which let
    anyone lock you out. Now failures count against the failing address only."""
    _, authenticator = make_user("alice", sign_in=False)
    main._failures["ip:203.0.113.9"] = [main.time.monotonic()] * main.MAX_FAILURES
    assert sign_in(client, authenticator).status_code == 200


def test_add_and_remove_passkeys(app, client, make_user):
    user, _ = make_user("alice")
    second = new_authenticator()
    options = client.post("/api/account/passkeys/options").json()
    # The phone is told which passkeys it already has, so it won't duplicate one.
    assert len(options["excludeCredentials"]) == 1
    assert client.post("/api/account/passkeys", json=second.create(options)).status_code == 200
    passkeys = client.get("/api/account/passkeys").json()["passkeys"]
    assert len(passkeys) == 2

    assert client.delete(f"/api/account/passkeys/{passkeys[0]['id']}").status_code == 200
    # The last one can't go - that would lock the account out.
    assert client.delete(f"/api/account/passkeys/{passkeys[1]['id']}").status_code == 400
    client.cookies.clear()
    assert sign_in(client, second).status_code == 200


def test_cam_home_redirects_to_the_public_address(client):
    r = client.get("/login?x=1", headers={"Host": "cam.home"}, follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == f"{config.PUBLIC_URL}/login?x=1"


def test_state_changes_need_the_same_origin_header(client):
    assert client.request("POST", "/api/login/options", same_origin=False).status_code == 403


def test_session_cookie_is_locked_down(app, client):
    _, token = asyncio.run(app.state.store.create_invite("alice", False, None, 24))
    r = accept(client, token, new_authenticator())
    cookie = [c for c in r.headers.get_list("set-cookie") if c.startswith("cam_session=")][0].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_security_headers(client):
    r = client.get("/login")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"


def test_account_from_before_passkeys_can_add_one(app, client, make_user):
    """An account made with a password has no passkey and no user handle;
    its still-valid session is enough to add a first passkey."""
    user, _ = make_user("joseph")
    store = app.state.store
    store.users[user["id"]].webauthn_id = None
    store.passkeys.clear()
    first = new_authenticator()
    options = client.post("/api/account/passkeys/options").json()
    assert client.post("/api/account/passkeys", json=first.create(options)).status_code == 200
    client.cookies.clear()
    assert sign_in(client, first).status_code == 200


def test_scripts_are_revalidated_on_every_load(client):
    """A cached old script outlived the password-to-passkey switch on a phone."""
    assert client.get("/static/login.js").headers["cache-control"] == "no-cache"


def test_an_old_page_is_told_to_reload(client, make_user):
    make_user("alice", sign_in=False)
    # What the password-era page sent, with no passkey sign-in started.
    r = client.post("/api/login", json={"username": "alice", "password": "hunter2hunter2"})
    assert r.status_code == 400 and "reload" in r.json()["detail"]
