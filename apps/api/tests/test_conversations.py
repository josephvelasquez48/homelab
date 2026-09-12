import json


def _sse_payloads(body: str) -> list[dict]:
    """Parse an SSE body into the JSON frames it carried."""
    return [
        json.loads(line[len("data: "):])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_create_and_list_conversation(client, auth_headers):
    created = client.post("/v1/conversations", json={}, headers=auth_headers).json()
    assert created["title"] is None

    listed = client.get("/v1/conversations", headers=auth_headers).json()
    assert [c["id"] for c in listed] == [created["id"]]


def test_missing_conversation_is_404(client, auth_headers):
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/v1/conversations/{missing}", headers=auth_headers).status_code == 404
    assert client.post(
        f"/v1/conversations/{missing}/messages", json={"content": "hi"}
    , headers=auth_headers).status_code == 404


def test_reply_streams_and_both_turns_persist(client, auth_headers):
    cid = client.post("/v1/conversations", json={}, headers=auth_headers).json()["id"]

    res = client.post(f"/v1/conversations/{cid}/messages", json={"content": "hello"}, headers=auth_headers)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")

    frames = _sse_payloads(res.text)
    assert [f["token"] for f in frames if "token" in f] == ["Hello", " there"]
    assert frames[-1]["done"] is True

    # The point of persisting: the next turn must be able to see both sides.
    messages = client.get(f"/v1/conversations/{cid}", headers=auth_headers).json()["messages"]
    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "hello"),
        ("assistant", "Hello there"),
    ]


def test_history_is_sent_in_chronological_order(client, auth_headers):
    """The DB query fetches newest-first to make LIMIT keep recent turns.

    The router reverses it. If that reversal is ever dropped the model gets
    the conversation backwards, which does not raise - it just answers
    strangely, which is far harder to diagnose than an exception.
    """
    cid = client.post("/v1/conversations", json={}, headers=auth_headers).json()["id"]
    client.post(f"/v1/conversations/{cid}/messages", json={"content": "first"}, headers=auth_headers)
    client.post(f"/v1/conversations/{cid}/messages", json={"content": "second"}, headers=auth_headers)

    ollama = client.fake_ollama
    chat_calls = [payload for url, payload in ollama.requests if url == "/api/chat"]
    sent = [m["content"] for m in chat_calls[-1]["messages"]]

    assert sent == ["first", "Hello there", "second"]


def test_first_message_becomes_the_title(client, auth_headers):
    cid = client.post("/v1/conversations", json={}, headers=auth_headers).json()["id"]
    client.post(f"/v1/conversations/{cid}/messages", json={"content": "what is dns"}, headers=auth_headers)

    assert client.get(f"/v1/conversations/{cid}", headers=auth_headers).json()["title"] == "what is dns"


def test_unreachable_model_reports_an_error_frame(client, auth_headers):
    """A failure has to arrive as a frame, not a dead connection.

    The response has already begun with HTTP 200 by the time generation
    starts, so there is no status code left to fail with. Without an explicit
    error frame the stream just stops and the UI cannot distinguish that from
    a very slow reply.
    """
    cid = client.post("/v1/conversations", json={}, headers=auth_headers).json()["id"]
    client.fake_ollama.fail_with = ConnectionError("refused")

    res = client.post(f"/v1/conversations/{cid}/messages", json={"content": "hi"}, headers=auth_headers)
    assert res.status_code == 200

    frames = _sse_payloads(res.text)
    assert "error" in frames[-1]
    assert "refused" in frames[-1]["error"]

    # Nothing was generated, so only the question should be stored - not an
    # empty assistant turn that would confuse the next request.
    messages = client.get(f"/v1/conversations/{cid}", headers=auth_headers).json()["messages"]
    assert [m["role"] for m in messages] == ["user"]


def test_empty_message_is_rejected(client, auth_headers):
    cid = client.post("/v1/conversations", json={}, headers=auth_headers).json()["id"]
    assert client.post(
        f"/v1/conversations/{cid}/messages", json={"content": ""}
    , headers=auth_headers).status_code == 422


def test_delete_removes_the_conversation(client, auth_headers):
    cid = client.post("/v1/conversations", json={}, headers=auth_headers).json()["id"]
    assert client.delete(f"/v1/conversations/{cid}", headers=auth_headers).status_code == 204
    assert client.get(f"/v1/conversations/{cid}", headers=auth_headers).status_code == 404


def test_conversations_default_to_the_general_model(client, auth_headers):
    """Not the coder model, which backs /v1/chat and the job worker.

    They are different jobs. qwen2.5-coder is tuned for completion and
    drifts toward emitting code unprompted, which in a conversation reads
    as the assistant ignoring what was asked.
    """
    from app.config import CHAT_MODEL

    created = client.post("/v1/conversations", json={}, headers=auth_headers).json()
    assert created["model"] == CHAT_MODEL
    assert "coder" not in created["model"]


def test_an_explicit_model_is_honoured(client, auth_headers):
    created = client.post(
        "/v1/conversations", json={"model": "qwen2.5-coder:7b"}, headers=auth_headers
    ).json()
    assert created["model"] == "qwen2.5-coder:7b"
