import asyncio

from app import main


def test_pages_need_a_session(client):
    for path in ["/", "/admin", "/account"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/me").status_code == 401


def test_invite_creates_the_account_and_signs_in(client, make_user):
    user = make_user("alice")
    assert user["username"] == "alice" and not user["is_admin"]
    assert client.get("/api/me").json()["user"]["username"] == "alice"
    assert client.get("/", follow_redirects=False).status_code == 200


def test_invite_link_works_once(app, client):
    _, token = asyncio.run(app.state.store.create_invite("bob", False, None, 7))
    assert client.get(f"/api/invite/{token}").json() == {"username": "bob", "reset": False}
    assert client.post(f"/api/invite/{token}", json={"password": "a long password"}).status_code == 200
    assert client.post(f"/api/invite/{token}", json={"password": "another password"}).status_code == 404
    assert client.get(f"/api/invite/{token}").status_code == 404


def test_short_passwords_are_refused(app, client):
    _, token = asyncio.run(app.state.store.create_invite("bob", False, None, 7))
    r = client.post(f"/api/invite/{token}", json={"password": "short"})
    assert r.status_code == 400
    # The link is still good after a refused password.
    assert client.get(f"/api/invite/{token}").status_code == 200


def test_login_and_logout(client, make_user):
    make_user("alice", password="correct horse battery", sign_in=False)
    assert client.post("/api/login", json={"username": "Alice", "password": "correct horse battery"}).status_code == 200
    assert client.get("/api/me").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401


def test_wrong_password_and_unknown_name_look_the_same(client, make_user):
    make_user("alice", sign_in=False)
    wrong = client.post("/api/login", json={"username": "alice", "password": "nope nope nope"})
    unknown = client.post("/api/login", json={"username": "nobody", "password": "nope nope nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_login_is_throttled(client, make_user):
    make_user("alice", password="correct horse battery", sign_in=False)
    for _ in range(main.MAX_FAILURES):
        client.post("/api/login", json={"username": "alice", "password": "wrong password!"})
    # Locked out even with the right password, until the window passes.
    r = client.post("/api/login", json={"username": "alice", "password": "correct horse battery"})
    assert r.status_code == 429


def test_state_changes_need_the_same_origin_header(client, make_user):
    make_user("alice", sign_in=False)
    r = client.request("POST", "/api/login", json={"username": "alice", "password": "correct horse battery"},
                       same_origin=False)
    assert r.status_code == 403


def test_session_cookie_is_locked_down(app, client):
    _, token = asyncio.run(app.state.store.create_invite("alice", False, None, 7))
    r = client.post(f"/api/invite/{token}", json={"password": "a long password"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_change_password_signs_out_other_browsers(app, client, make_user):
    make_user("alice", password="correct horse battery", sign_in=False)
    client.post("/api/login", json={"username": "alice", "password": "correct horse battery"})
    other = client.cookies.get("cam_session")
    client.cookies.clear()
    client.post("/api/login", json={"username": "alice", "password": "correct horse battery"})

    r = client.post("/api/account/password", json={"current": "correct horse battery", "new": "an even better one"})
    assert r.status_code == 200
    assert client.get("/api/me").status_code == 200          # this browser stays in
    client.cookies.set("cam_session", other)
    assert client.get("/api/me").status_code == 401          # the other one is out


def test_change_password_needs_the_current_one(client, make_user):
    make_user("alice")
    r = client.post("/api/account/password", json={"current": "wrong wrong wrong", "new": "an even better one"})
    assert r.status_code == 400


def test_security_headers(client):
    r = client.get("/login")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"
