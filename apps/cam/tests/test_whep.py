import httpx

SDP = {"Content-Type": "application/sdp"}


def test_offer_needs_a_session(client):
    assert client.post("/api/whep", content=b"v=0", headers=SDP).status_code == 401


def test_offer_is_relayed_and_logged(app, client, make_user):
    make_user("alice")
    r = client.post("/api/whep", content=b"v=0\r\noffer\r\n", headers=SDP)
    assert r.status_code == 201
    assert r.content == b"v=0\r\nfake-answer\r\n"
    # The browser is pointed at this app for ending it, never at the Mac.
    assert r.headers["location"] == "/api/whep/5f0c2b7e-1111-2222-3333-444455556666"
    assert app.state.mediamtx.offers == [("/cam/whep", b"v=0\r\noffer\r\n")]
    assert [v["username"] for v in app.state.store.views] == ["alice"]


def test_only_the_owner_can_end_a_stream(app, client, make_user):
    make_user("alice")
    location = client.post("/api/whep", content=b"v=0", headers=SDP).headers["location"]
    client.cookies.clear()
    make_user("bob")
    assert client.delete(location).status_code == 403
    assert app.state.mediamtx.deleted == []


def test_owner_ends_the_stream(app, client, make_user):
    make_user("alice")
    location = client.post("/api/whep", content=b"v=0", headers=SDP).headers["location"]
    assert client.delete(location).status_code == 204
    assert app.state.mediamtx.deleted == ["/cam/whep/5f0c2b7e-1111-2222-3333-444455556666"]


def test_mac_unreachable_reads_as_offline(app, client, make_user):
    make_user("alice")
    app.state.mediamtx.fail_with = httpx.ConnectError("timed out")
    r = client.post("/api/whep", content=b"v=0", headers=SDP)
    assert r.status_code == 503
    assert "offline" in r.json()["detail"]


def test_camera_not_streaming(app, client, make_user):
    make_user("alice")
    app.state.mediamtx.status = 404
    assert client.post("/api/whep", content=b"v=0", headers=SDP).status_code == 503


def test_offer_must_be_sdp(client, make_user):
    make_user("alice")
    assert client.post("/api/whep", json={"sdp": "v=0"}).status_code == 415
