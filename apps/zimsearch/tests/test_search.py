"""Tests for the retrieval service.

These exercise the cascade logic with stub archives rather than a real ZIM.
A real archive would make the tests depend on a 127GB file that may still be
downloading, and the logic worth testing here is the fallthrough, not
libzim's search quality.
"""
import importlib

import pytest


class StubResult:
    def __init__(self, paths):
        self._paths = paths

    def getResults(self, start, count):
        return self._paths[start:start + count]


class StubSearcher:
    registry: dict = {}

    def __init__(self, archive):
        self.archive = archive

    def search(self, query):
        return StubResult(self.archive.paths)


class StubQuery:
    def set_query(self, q):
        self.q = q
        return self


class StubEntry:
    def __init__(self, title):
        self.title = title

    def get_item(self):
        class Item:
            content = b"<p>Body text about " + title_bytes(self.title) + b".</p>"
        return Item()


def title_bytes(t):
    return t.encode()


class StubArchive:
    def __init__(self, paths):
        self.paths = paths

    def get_entry_by_path(self, path):
        return StubEntry(path)


@pytest.fixture
def mod(monkeypatch):
    import app.main as m
    importlib.reload(m)
    monkeypatch.setattr(m, "Searcher", StubSearcher)
    monkeypatch.setattr(m, "ZimQuery", StubQuery)
    m._archives.clear()
    return m


def call(mod, **kwargs):
    """Invoke the endpoint with real values, not FastAPI Query defaults."""
    import asyncio

    params = {"q": "x", "k": 3, "chars": 500, "book": None, "candidates": 0}
    params.update(kwargs)
    return asyncio.run(mod.search(**params))


def test_concise_archive_answers_when_it_has_the_topic(mod):
    mod._archives.update({
        "wikipedia_en_simple_all_x": StubArchive(["Photosynthesis", "Ecosystem"]),
        "wikipedia_en_all_maxi_x": StubArchive(["Photosynthesis", "Calvin cycle", "Chlorophyll"]),
    })

    result = call(mod)

    assert result["answered_by"] == "wikipedia_en_simple_all_x"
    assert len(result["tried"]) == 1  # the full archive was never consulted


def test_thin_results_fall_through_to_the_full_archive(mod):
    """The point of the cascade: coverage, not just brevity."""
    mod._archives.update({
        "wikipedia_en_simple_all_x": StubArchive(["Obscure"]),        # 1 hit, below MIN_HITS
        "wikipedia_en_all_maxi_x": StubArchive(["Obscure", "Detail", "More"]),
    })

    result = call(mod)

    assert result["answered_by"] == "wikipedia_en_all_maxi_x"
    assert len(result["tried"]) == 2


def test_thin_results_are_kept_when_nothing_follows(mod):
    """With no archive left, few results beat none."""
    mod._archives.update({"wikipedia_en_simple_all_x": StubArchive(["Only"])})

    result = call(mod)

    assert result["answered_by"] == "wikipedia_en_simple_all_x"
    assert len(result["results"]) == 1


def test_no_matches_anywhere_reports_honestly(mod):
    """Empty must not be indistinguishable from unanswered."""
    mod._archives.update({"wikipedia_en_simple_all_x": StubArchive([])})

    result = call(mod)

    assert result["answered_by"] is None
    assert result["results"] == []


def test_candidates_overfetch_without_widening_the_answer(mod):
    """The reranking seam: fetch many, return k."""
    mod._archives.update({
        "wikipedia_en_simple_all_x": StubArchive([f"A{i}" for i in range(30)]),
    })

    result = call(mod, k=2, candidates=30)

    assert len(result["results"]) == 2
    assert "30 hits" in result["tried"][0]
    assert result["reranked"] is False


def test_incomplete_archives_are_skipped(mod, tmp_path, monkeypatch):
    """A partially downloaded archive can open and return wrong results."""
    (tmp_path / "done.zim").write_bytes(b"")
    (tmp_path / "downloading.zim").write_bytes(b"")
    (tmp_path / "downloading.zim.aria2").write_bytes(b"")

    opened = []
    monkeypatch.setattr(mod, "ZIM_DIR", tmp_path)
    monkeypatch.setattr(mod, "Archive", lambda p: opened.append(p) or StubArchive([]))
    mod._archives.clear()
    mod._load()

    assert any("done.zim" in p for p in opened)
    assert not any("downloading.zim" in p for p in opened)
