import json


def _frames(body: str) -> list[dict]:
    return [
        json.loads(line[len("data: "):])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_page_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Homelab Chat" in res.text


def test_api_key_never_reaches_the_browser(client):
    """The reason this app exists rather than the page calling api directly.

    A key in page JavaScript is readable by anyone who can load the page,
    which on a LAN is everyone.
    """
    page = client.get("/").text
    assert "test-api-key" not in page
    assert "X-API-Key" not in page


def test_conversation_calls_are_forwarded(client):
    client.fake_api.next_response.__init__(200, [{"id": "abc", "title": "t"}])
    assert client.get("/api/conversations").json() == [{"id": "abc", "title": "t"}]
    assert client.fake_api.calls[-1][:2] == ("GET", "/v1/conversations")


def test_stream_is_relayed_unbuffered(client):
    res = client.post("/api/conversations/abc/messages", json={"content": "hi"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert _frames(res.text) == [{"token": "Hi"}, {"done": True}]


def test_upstream_error_becomes_a_frame(client):
    """A stream that has already started cannot fail with a status code.

    Returning 200 and then going silent would be indistinguishable from a
    slow model, so an upstream failure has to arrive as content.
    """
    client.fake_api.stream_status = 500

    res = client.post("/api/conversations/abc/messages", json={"content": "hi"})
    assert res.status_code == 200
    frames = _frames(res.text)
    assert "error" in frames[-1]
    assert "500" in frames[-1]["error"]


def test_unreachable_api_becomes_a_frame(client):
    import httpx

    client.fake_api.raise_on_stream = httpx.ConnectError("no route")

    frames = _frames(client.post("/api/conversations/abc/messages", json={"content": "hi"}).text)
    assert "cannot reach the api service" in frames[-1]["error"]


def test_model_timeout_says_what_to_check(client):
    """A timeout here has a specific, likely cause worth naming."""
    import httpx

    client.fake_api.raise_on_stream = httpx.ReadTimeout("slow")

    frames = _frames(client.post("/api/conversations/abc/messages", json={"content": "hi"}).text)
    assert "GPU host awake" in frames[-1]["error"]
