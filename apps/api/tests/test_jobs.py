def test_create_job_returns_pending(client, auth_headers):
    r = client.post("/jobs", json={"type": "chat", "payload": {"message": "hi"}}, headers=auth_headers)
    assert r.status_code == 202
    data = r.json()
    assert data["status"] == "pending"
    assert data["id"]


def test_create_job_rejects_unknown_type(client, auth_headers):
    r = client.post("/jobs", json={"type": "bogus", "payload": {}}, headers=auth_headers)
    assert r.status_code == 422


def test_get_job_roundtrip(client, auth_headers):
    created = client.post(
        "/jobs", json={"type": "chat", "payload": {"message": "hi"}}, headers=auth_headers
    ).json()

    r = client.get(f"/jobs/{created['id']}", headers=auth_headers)

    assert r.status_code == 200
    data = r.json()
    assert data["id"] == created["id"]
    assert data["status"] == "pending"
    assert data["payload"] == {"message": "hi"}


def test_get_unknown_job_is_404(client, auth_headers):
    r = client.get("/jobs/does-not-exist", headers=auth_headers)
    assert r.status_code == 404
