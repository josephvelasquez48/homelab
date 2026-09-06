from app.config import RATE_LIMIT_PER_MINUTE


def test_chat_rejects_missing_key(client):
    r = client.post("/v1/chat", json={"message": "hi"})
    assert r.status_code == 401


def test_chat_rejects_wrong_key(client):
    r = client.post("/v1/chat", json={"message": "hi"}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_chat_rejects_non_ascii_key(client):
    """A high byte in the header must be a 401, not an unhandled 500.

    Header values arrive latin-1 decoded, and secrets.compare_digest
    raises TypeError on non-ASCII str - so before this was compared as
    bytes, any unauthenticated client could turn the auth check into a
    500 just by sending one.
    """
    # Sent as raw bytes: httpx refuses to encode a non-ASCII str header,
    # but nothing stops a raw socket or curl from putting a high byte on
    # the wire, and Starlette decodes it latin-1 on the way in.
    r = client.post(
        "/v1/chat", json={"message": "hi"}, headers={"X-API-Key": b"wr\xf6ng"}
    )
    assert r.status_code == 401


def test_chat_rejects_empty_key_against_empty_configured_key(client, monkeypatch):
    """compare_digest("", "") is True, so an unset key must not fail open."""
    from app import auth

    monkeypatch.setattr(auth, "API_KEY", "")

    r = client.post("/v1/chat", json={"message": "hi"}, headers={"X-API-Key": ""})
    assert r.status_code == 401


def test_chat_accepts_correct_key(client, auth_headers):
    r = client.post("/v1/chat", json={"message": "hi"}, headers=auth_headers)
    assert r.status_code == 200


def test_health_does_not_require_auth(client):
    r = client.get("/health")
    assert r.status_code == 200


def test_rate_limit_trips_after_configured_max(client, auth_headers):
    for _ in range(RATE_LIMIT_PER_MINUTE):
        r = client.get(f"/jobs/{'0' * 8}", headers=auth_headers)
        assert r.status_code in (200, 404)  # not-found is fine, 401/429 are not

    r = client.get(f"/jobs/{'0' * 8}", headers=auth_headers)
    assert r.status_code == 429
