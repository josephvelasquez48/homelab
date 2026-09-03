def test_chat_returns_generated_response(client, auth_headers):
    r = client.post("/v1/chat", json={"message": "hello"}, headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["response"] == "fake response"
    assert data["cached"] is False
    assert data["tokens_per_sec"] == 100.0  # 10 tokens / 0.1s


def test_chat_second_identical_call_is_cached(client, auth_headers):
    client.post("/v1/chat", json={"message": "repeat me"}, headers=auth_headers)
    calls_before = len(client.fake_ollama.requests)

    r = client.post("/v1/chat", json={"message": "repeat me"}, headers=auth_headers)

    assert r.status_code == 200
    assert r.json()["cached"] is True
    assert len(client.fake_ollama.requests) == calls_before  # no new Ollama call
