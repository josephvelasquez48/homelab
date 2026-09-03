def test_embed_single_string(client, auth_headers):
    r = client.post("/v1/embed", json={"input": "hello"}, headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert len(data["embeddings"]) == 1
    assert data["dimensions"] == 3


def test_embed_batch(client, auth_headers):
    r = client.post("/v1/embed", json={"input": ["a", "b", "c"]}, headers=auth_headers)
    assert r.status_code == 200
    assert len(r.json()["embeddings"]) == 3


def test_embed_rejects_empty_input(client, auth_headers):
    r = client.post("/v1/embed", json={"input": []}, headers=auth_headers)
    assert r.status_code == 422


def test_rag_query_retrieves_ingested_document(client, auth_headers):
    client.post(
        "/v1/documents",
        json={"content": "The sky is blue because of Rayleigh scattering."},
        headers=auth_headers,
    )

    r = client.post("/v1/rag/query", json={"question": "Why is the sky blue?"}, headers=auth_headers)

    assert r.status_code == 200
    data = r.json()
    assert len(data["sources"]) == 1
    assert "Rayleigh scattering" in data["sources"][0]["content"]
    assert data["answer"] == "fake response"


def test_rag_query_with_no_documents_still_answers(client, auth_headers):
    r = client.post("/v1/rag/query", json={"question": "anything"}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["sources"] == []
