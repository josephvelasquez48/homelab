from tests.conftest import accept, new_authenticator, sign_in


def test_only_admins_reach_admin(client, make_user):
    make_user("alice", is_admin=False)
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/"


def test_invite_makes_a_working_link(client, make_user):
    make_user("admin", is_admin=True)
    r = client.post("/api/admin/invites", json={"username": "Maria", "is_admin": False})
    assert r.status_code == 200
    url = r.json()["url"]
    assert url.startswith("https://cam.example.test/invite/")
    token = url.rsplit("/", 1)[1]
    assert client.get(f"/api/invite/{token}").json()["username"] == "maria"
    assert client.get("/api/admin/users").json()["invite_hours"] == 24


def test_invite_refuses_bad_and_taken_names(client, make_user):
    make_user("admin", is_admin=True)
    assert client.post("/api/admin/invites", json={"username": "a b"}).status_code == 400
    assert client.post("/api/admin/invites", json={"username": "x"}).status_code == 400
    assert client.post("/api/admin/invites", json={"username": "admin"}).status_code == 409


def test_reset_revokes_old_passkeys_and_sessions(client, make_user):
    make_user("admin", is_admin=True)
    admin_cookie = client.cookies.get("cam_session")
    client.cookies.clear()
    bob, bobs_phone = make_user("bob")
    bob_cookie = client.cookies.get("cam_session")

    client.cookies.set("cam_session", admin_cookie)
    token = client.post(f"/api/admin/users/{bob['id']}/reset").json()["url"].rsplit("/", 1)[1]
    assert client.get(f"/api/invite/{token}").json() == {"username": "bob", "reset": True}

    client.cookies.clear()
    bobs_new_phone = new_authenticator()
    assert accept(client, token, bobs_new_phone).status_code == 200
    client.cookies.set("cam_session", bob_cookie)
    assert client.get("/api/me").status_code == 401           # old session gone
    client.cookies.clear()
    assert sign_in(client, bobs_phone).status_code == 401      # lost phone's passkey dead
    assert sign_in(client, bobs_new_phone).status_code == 200


def test_turning_off_signs_out_and_blocks_sign_in(client, make_user):
    make_user("admin", is_admin=True)
    admin_cookie = client.cookies.get("cam_session")
    client.cookies.clear()
    bob, bobs_phone = make_user("bob")
    bob_cookie = client.cookies.get("cam_session")

    client.cookies.set("cam_session", admin_cookie)
    assert client.post(f"/api/admin/users/{bob['id']}/disable").status_code == 200

    client.cookies.set("cam_session", bob_cookie)
    assert client.get("/api/me").status_code == 401
    client.cookies.clear()
    assert sign_in(client, bobs_phone).status_code == 401


def test_admins_cant_lock_themselves_out(client, make_user):
    admin, _ = make_user("admin", is_admin=True)
    assert client.post(f"/api/admin/users/{admin['id']}/disable").status_code == 400
    assert client.delete(f"/api/admin/users/{admin['id']}").status_code == 400


def test_delete_and_revoke(client, make_user):
    make_user("admin", is_admin=True)
    admin_cookie = client.cookies.get("cam_session")
    client.cookies.clear()
    bob, bobs_phone = make_user("bob")
    client.cookies.set("cam_session", admin_cookie)

    assert client.delete(f"/api/admin/users/{bob['id']}").status_code == 200
    users = client.get("/api/admin/users").json()["users"]
    assert [u["username"] for u in users] == ["admin"]
    assert users[0]["passkeys"] == 1
    client.cookies.clear()
    assert sign_in(client, bobs_phone).status_code == 401      # deleting removed the passkey too

    client.cookies.set("cam_session", admin_cookie)
    invite = client.post("/api/admin/invites", json={"username": "carol"}).json()
    assert client.delete(f"/api/admin/invites/{invite['invite']['id']}").status_code == 200
    token = invite["url"].rsplit("/", 1)[1]
    assert client.get(f"/api/invite/{token}").status_code == 404
