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


def test_ready_stays_ready_when_ollama_unreachable(client):
    """A sleeping desktop must not take the whole API out of the Service.

    This assertion is the reverse of what it used to be. Readiness once
    failed here, on the reasoning that a replica unable to reach Ollama
    should have traffic routed elsewhere. There is no elsewhere: Ollama
    runs on one desktop, so every replica failed together, the Deployment
    went unready, and Argo CD reported the app Degraded because a PC had
    gone to sleep - taking every non-inference route down with it.
    """
    client.fake_ollama.fail_with = ConnectionError(
        "can't connect to remote host (192.168.1.131): Connection refused"
    )

    r = client.get("/ready")
    assert r.status_code == 200

    body = r.json()
    # Still reported, so the information is moved rather than lost.
    assert "Connection refused" in body["ollama"]
    assert body["postgres"] == "ok"
    assert body["redis"] == "ok"


def test_inference_gauge_tracks_ollama(client):
    """The metric is what replaces the readiness failure as the signal."""
    client.get("/ready")
    assert b"homelab_inference_reachable 1.0" in client.get("/metrics").content

    client.fake_ollama.fail_with = ConnectionError("refused")
    client.get("/ready")
    assert b"homelab_inference_reachable 0.0" in client.get("/metrics").content

def test_health_ignores_ollama(client):
    """Liveness must not depend on Ollama.

    /health backs the liveness probe. If it failed here, an Ollama
    outage would restart every api replica for its duration - and
    restarting cannot make Ollama reachable.
    """
    client.fake_ollama.fail_with = ConnectionError("refused")

    r = client.get("/health")
    assert r.status_code == 200


def test_ready_still_fails_on_real_dependencies(client):
    """Ollama is exempt; Postgres and Redis are not.

    Those two are in-cluster and have somewhere to route to, and a replica
    that cannot reach them genuinely cannot serve.
    """
    client.fake_ollama.fail_with = ConnectionError("refused")
    client.fake_redis.fail_ping = True

    r = client.get("/ready")
    assert r.status_code == 503
    assert r.json()["detail"]["unready"] == ["redis"]
