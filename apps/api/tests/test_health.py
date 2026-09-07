def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "postgres": "ok", "redis": "ok"}


def test_metrics_exposed(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert b"http_requests_total" in r.content


def test_ready_ok(client):
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "postgres": "ok",
        "redis": "ok",
        "ollama": "ok",
    }


def test_ready_fails_when_ollama_unreachable(client):
    """The desktop-node case: Postgres and Redis fine, Ollama refused.

    This is the whole reason /ready exists. Such a replica passes
    /health, joins the Service, and then fails every /v1/chat routed to
    it. Readiness has to reject it.
    """
    client.fake_ollama.fail_with = ConnectionError(
        "can't connect to remote host (192.168.1.133): Connection refused"
    )

    r = client.get("/ready")
    assert r.status_code == 503

    detail = r.json()["detail"]
    assert detail["unready"] == ["ollama"]
    assert "Connection refused" in detail["ollama"]
    # The reachable dependencies still report ok, so the probe failure
    # says which one is broken rather than just that something is.
    assert detail["postgres"] == "ok"
    assert detail["redis"] == "ok"


def test_health_ignores_ollama(client):
    """Liveness must not depend on Ollama.

    /health backs the liveness probe. If it failed here, an Ollama
    outage would restart every api replica for its duration - and
    restarting cannot make Ollama reachable.
    """
    client.fake_ollama.fail_with = ConnectionError("refused")

    r = client.get("/health")
    assert r.status_code == 200


def test_ready_reports_every_failure_at_once(client):
    """"This node reaches nothing" should not look like "ollama is down"."""
    client.fake_ollama.fail_with = ConnectionError("refused")
    client.fake_redis.fail_ping = True

    r = client.get("/ready")
    assert r.status_code == 503
    assert set(r.json()["detail"]["unready"]) == {"redis", "ollama"}
