from app.config import RATE_LIMIT_PER_MINUTE


def test_chat_rejects_missing_key(client):
    r = client.post("/v1/chat", json={"message": "hi"})
    assert r.status_code == 401


def test_chat_rejects_wrong_key(client):
    r = client.post("/v1/chat", json={"message": "hi"}, headers={"X-API-Key": "wrong"})
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
