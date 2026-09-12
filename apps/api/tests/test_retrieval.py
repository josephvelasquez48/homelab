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
    prompt = sent["messages"][0]["content"]
    assert "Ada Lovelace wrote the first algorithm" in prompt
    # The bracket markers are what the page turns into links, so asking for
    # them is not a stylistic preference.
    assert "square brackets, like [1]" in prompt
    # Without this the model is free to invent a citation, which looks
    # exactly like the checkable answer retrieval exists to produce.
    assert "Never put a number on a sentence the passages do not support" in prompt


def test_sources_arrive_as_their_own_frame(client, auth_headers):
    """Not trailing tokens: the page renders them as a panel, not as more prose."""
    client.fake_zimsearch.results = [PASSAGE]
    _, res = _reply(client, auth_headers, "who was Ada Lovelace", retrieve=True)

    frames = _frames(res.text)
    assert [f["token"] for f in frames if "token" in f] == ["Hello", " there"]
    source = [f for f in frames if "sources" in f][0]["sources"][0]
    assert source == {
        "n": 1,
        "title": "Ada Lovelace",
        "archive": "wikipedia_en_simple_all",
        "url": PASSAGE["url"],
        "excerpt": PASSAGE["text"],
    }


def test_sources_survive_reopening_the_conversation(client, auth_headers):
    """The point of a column rather than a footer: they are still there later."""
    client.fake_zimsearch.results = [PASSAGE]
    cid, _ = _reply(client, auth_headers, "who was Ada Lovelace", retrieve=True)

    stored = client.get(f"/v1/conversations/{cid}", headers=auth_headers).json()["messages"]
    assistant = [m for m in stored if m["role"] == "assistant"][0]
    # The citations are no longer glued onto the answer, so the text the
    # model produced is the text that is kept.
    assert assistant["content"] == "Hello there"
    assert [s["url"] for s in assistant["sources"]] == [PASSAGE["url"]]
    # A question never carries sources, only an answer does.
    assert [m for m in stored if m["role"] == "user"][0]["sources"] is None


def test_a_turn_that_did_not_search_has_no_sources(client, auth_headers):
    """Null, not an empty list. "Did not look" is not "looked and found nothing"."""
    cid, _ = _reply(client, auth_headers, "hello")
    stored = client.get(f"/v1/conversations/{cid}", headers=auth_headers).json()["messages"]
    assert all(m["sources"] is None for m in stored)


def test_a_long_passage_is_cut_on_a_word(client, auth_headers):
    """A mid-word cut reads as a rendering bug rather than a deliberate excerpt."""
    long_passage = dict(PASSAGE, text="Ada Lovelace " * 400)
    client.fake_zimsearch.results = [long_passage]
    _, res = _reply(client, auth_headers, "who was Ada Lovelace", retrieve=True)

    excerpt = [f for f in _frames(res.text) if "sources" in f][0]["sources"][0]["excerpt"]
    assert excerpt.endswith("...")
    assert len(excerpt) <= 404
    assert not excerpt.replace("...", "").endswith(("Ad", "Lov", "Lovelac"))


def test_a_missing_archive_still_answers(client, auth_headers):
    """The kiwix pod being down costs the citations, not the reply."""
    client.fake_zimsearch.fail_with = ConnectionError("no route to zimsearch")
    cid, res = _reply(client, auth_headers, "who was Ada Lovelace", retrieve=True)

    frames = _frames(res.text)
    assert [f["token"] for f in frames if "token" in f] == ["Hello", " there"]
    assert not [f for f in frames if "sources" in f]

    stored = client.get(f"/v1/conversations/{cid}", headers=auth_headers).json()["messages"]
    assert [m["content"] for m in stored if m["role"] == "assistant"] == ["Hello there"]


def test_nothing_found_adds_nothing(client, auth_headers):
    """An empty result set is not a failure, and must not produce an empty footer."""
    client.fake_zimsearch.results = []
    _, res = _reply(client, auth_headers, "an obscure thing", retrieve=True)

    assert client.fake_zimsearch.queries, "the search should still have been attempted"
    assert not [f for f in _frames(res.text) if "sources" in f]
    sent = [r for r in client.fake_ollama.requests if r[0] == "/api/chat"][-1][1]
    assert [m["role"] for m in sent["messages"]] == ["user"]


def test_the_question_is_what_gets_searched(client, auth_headers):
    client.fake_zimsearch.results = [PASSAGE]
    _reply(client, auth_headers, "who was Ada Lovelace", retrieve=True)

    url, params = client.fake_zimsearch.queries[-1]
    assert url == "/search"
    assert params["q"] == "who was Ada Lovelace"
