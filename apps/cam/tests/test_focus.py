import httpx


def test_focus_is_admins_only(app, client, make_user):
    make_user("alice")
    assert client.get("/api/focus").status_code == 403
    assert client.post("/api/focus", json={"focus": 100}).status_code == 403
    assert app.state.focus.requests == []


def test_focus_needs_a_session(client):
    assert client.get("/api/focus").status_code == 401


def test_admin_reads_and_sets_focus(app, client, make_user):
    make_user("demo", is_admin=True)
    assert client.get("/api/focus").json()["auto"] is True
    r = client.post("/api/focus", json={"focus": 120})
    assert r.status_code == 200
    assert r.json()["auto"] is False and r.json()["focus"] == 120
    assert client.post("/api/focus", json={"auto": True}).json()["auto"] is True
    assert app.state.focus.requests[1:] == [("PUT", "/focus", {"focus": 120}), ("PUT", "/focus", {"auto": True})]


def test_focus_needs_the_same_origin_header(app, client, make_user):
    make_user("demo", is_admin=True)
    assert client.request("POST", "/api/focus", json={"focus": 120}, same_origin=False).status_code == 403


def test_focus_values_are_checked(app, client, make_user):
    make_user("demo", is_admin=True)
    for body in [{"focus": 251}, {"focus": -5}, {"focus": "120"}, {"focus": 12.5}, {"focus": True}, {"auto": False}, {}]:
        assert client.post("/api/focus", json=body).status_code == 400, body
    assert app.state.focus.requests == []


def test_mac_unreachable_reads_as_offline(app, client, make_user):
    make_user("demo", is_admin=True)
    app.state.focus.fail_with = httpx.ConnectError("timed out")
    r = client.post("/api/focus", json={"focus": 120})
    assert r.status_code == 503
    assert "offline" in r.json()["detail"]
