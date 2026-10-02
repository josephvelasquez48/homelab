import asyncio


def test_only_admins_reach_admin(client, make_user):
    make_user("alice", is_admin=False)
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/"


def test_invite_makes_a_working_link(client, make_user):
    make_user("admin", is_admin=True)
    r = client.post("/api/admin/invites", json={"username": "Maria", "is_admin": False})
    assert r.status_code == 200
    url = r.json()["url"]
    assert url.startswith("https://cam.home/invite/")
    token = url.rsplit("/", 1)[1]
    assert client.get(f"/api/invite/{token}").json()["username"] == "maria"


def test_invite_refuses_bad_and_taken_names(client, make_user):
    make_user("admin", is_admin=True)
    assert client.post("/api/admin/invites", json={"username": "a b"}).status_code == 400
    assert client.post("/api/admin/invites", json={"username": "x"}).status_code == 400
    assert client.post("/api/admin/invites", json={"username": "admin"}).status_code == 409


def test_reset_link_replaces_the_password_and_signs_out(app, client, make_user):
    make_user("admin", is_admin=True)
    admin_cookie = client.cookies.get("cam_session")
    client.cookies.clear()
    bob = make_user("bob", password="bobs old password")
    bob_cookie = client.cookies.get("cam_session")

    client.cookies.set("cam_session", admin_cookie)
    r = client.post(f"/api/admin/users/{bob['id']}/reset")
    token = r.json()["url"].rsplit("/", 1)[1]
    assert client.get(f"/api/invite/{token}").json() == {"username": "bob", "reset": True}

    client.cookies.clear()
    client.post(f"/api/invite/{token}", json={"password": "bobs new password"})
    client.cookies.set("cam_session", bob_cookie)
    assert client.get("/api/me").status_code == 401          # old session gone
    client.cookies.clear()
    assert client.post("/api/login", json={"username": "bob", "password": "bobs old password"}).status_code == 401
    assert client.post("/api/login", json={"username": "bob", "password": "bobs new password"}).status_code == 200


def test_turning_off_signs_out_and_blocks_login(client, make_user):
    make_user("admin", is_admin=True)
    admin_cookie = client.cookies.get("cam_session")
    client.cookies.clear()
    bob = make_user("bob", password="bobs password!")
    bob_cookie = client.cookies.get("cam_session")

    client.cookies.set("cam_session", admin_cookie)
    assert client.post(f"/api/admin/users/{bob['id']}/disable").status_code == 200

    client.cookies.set("cam_session", bob_cookie)
    assert client.get("/api/me").status_code == 401
    client.cookies.clear()
    assert client.post("/api/login", json={"username": "bob", "password": "bobs password!"}).status_code == 401


def test_admins_cant_lock_themselves_out(client, make_user):
    admin = make_user("admin", is_admin=True)
    assert client.post(f"/api/admin/users/{admin['id']}/disable").status_code == 400
    assert client.delete(f"/api/admin/users/{admin['id']}").status_code == 400


def test_delete_and_revoke(app, client, make_user):
    make_user("admin", is_admin=True)
    admin_cookie = client.cookies.get("cam_session")
    client.cookies.clear()
    bob = make_user("bob")
    client.cookies.set("cam_session", admin_cookie)

    assert client.delete(f"/api/admin/users/{bob['id']}").status_code == 200
    names = [u["username"] for u in client.get("/api/admin/users").json()["users"]]
    assert names == ["admin"]

    invite = client.post("/api/admin/invites", json={"username": "carol"}).json()
    assert client.delete(f"/api/admin/invites/{invite['invite']['id']}").status_code == 200
    token = invite["url"].rsplit("/", 1)[1]
    assert client.get(f"/api/invite/{token}").status_code == 404
