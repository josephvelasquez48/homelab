"""Retrieval is optional, and the interesting cases are when it is absent.

A lookup that works is the easy path. What matters is that a turn still
produces an answer when the archive is not there, and that the citations
the model is shown are the ones the user gets told about.
"""
import json


def _frames(body: str) -> list[dict]:
    return [
        json.loads(line[len("data: "):])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def _reply(client, auth_headers, content, **body):
    cid = client.post("/v1/conversations", json={}, headers=auth_headers).json()["id"]
    res = client.post(
        f"/v1/conversations/{cid}/messages",
        json={"content": content, **body},
        headers=auth_headers,
    )
    return cid, res


PASSAGE = {
    "archive": "wikipedia_en_simple_all",
    "title": "Ada Lovelace",
    "path": "A/Ada_Lovelace",
    "url": "https://wikipedia.home/content/wikipedia_en_simple_all/A/Ada_Lovelace",
    "text": "Ada Lovelace wrote the first algorithm intended for a machine.",
    "truncated": False,
}


def test_no_lookup_unless_asked(client, auth_headers):
    """The default is off, so ordinary conversation costs no search."""
    _reply(client, auth_headers, "say that again shorter")
    assert client.fake_zimsearch.queries == []


def test_passages_are_given_to_the_model_before_the_question(client, auth_headers):
    """Placement is the point: a 7B model reads the end of its context hardest."""
    client.fake_zimsearch.results = [PASSAGE]
    _reply(client, auth_headers, "who was Ada Lovelace", retrieve=True)

    sent = [r for r in client.fake_ollama.requests if r[0] == "/api/chat"][-1][1]
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "user"], "passages must sit directly before the question"
    assert "Ada Lovelace wrote the first algorithm" in sent["messages"][0]["content"]
    # Without this the model is free to invent a citation, which looks
    # exactly like the checkable answer retrieval exists to produce.
    assert "Do not cite a passage that does not support" in sent["messages"][0]["content"]


def test_sources_are_stored_with_the_reply(client, auth_headers):
    """Citations live in the message text, so reopening the conversation keeps them."""
    client.fake_zimsearch.results = [PASSAGE]
    cid, res = _reply(client, auth_headers, "who was Ada Lovelace", retrieve=True)

    assert PASSAGE["url"] in res.text
    stored = client.get(f"/v1/conversations/{cid}", headers=auth_headers).json()["messages"]
    assistant = [m for m in stored if m["role"] == "assistant"][0]
    assert assistant["content"].startswith("Hello there")
    assert assistant["content"].endswith(f"[1] Ada Lovelace - {PASSAGE['url']}")


def test_a_missing_archive_still_answers(client, auth_headers):
    """The kiwix pod being down costs the citations, not the reply."""
    client.fake_zimsearch.fail_with = ConnectionError("no route to zimsearch")
    cid, res = _reply(client, auth_headers, "who was Ada Lovelace", retrieve=True)

    frames = _frames(res.text)
    assert [f["token"] for f in frames if "token" in f] == ["Hello", " there"]
    assert "Sources consulted" not in res.text

    stored = client.get(f"/v1/conversations/{cid}", headers=auth_headers).json()["messages"]
    assert [m["content"] for m in stored if m["role"] == "assistant"] == ["Hello there"]


def test_nothing_found_adds_nothing(client, auth_headers):
    """An empty result set is not a failure, and must not produce an empty footer."""
    client.fake_zimsearch.results = []
    _, res = _reply(client, auth_headers, "an obscure thing", retrieve=True)

    assert client.fake_zimsearch.queries, "the search should still have been attempted"
    assert "Sources consulted" not in res.text
    sent = [r for r in client.fake_ollama.requests if r[0] == "/api/chat"][-1][1]
    assert [m["role"] for m in sent["messages"]] == ["user"]


def test_the_question_is_what_gets_searched(client, auth_headers):
    client.fake_zimsearch.results = [PASSAGE]
    _reply(client, auth_headers, "who was Ada Lovelace", retrieve=True)

    url, params = client.fake_zimsearch.queries[-1]
    assert url == "/search"
    assert params["q"] == "who was Ada Lovelace"
