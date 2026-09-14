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
    # What reached the index, so a test can check the query and not only
    # the answer that came back.
    queries: list = []

    def __init__(self, archive):
        self.archive = archive

    def search(self, query):
        StubSearcher.queries.append(query.q)
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

    result = call(mod, q="how does photosynthesis work")

    assert result["answered_by"] == "wikipedia_en_simple_all_x"
    assert len(result["tried"]) == 1  # the full archive was never consulted


def test_thin_results_fall_through_to_the_full_archive(mod):
    """The point of the cascade: coverage, not just brevity."""
    mod._archives.update({
        "wikipedia_en_simple_all_x": StubArchive(["Obscure"]),        # 1 hit, below MIN_HITS
        "wikipedia_en_all_maxi_x": StubArchive(["Obscure", "Detail", "More"]),
    })

    result = call(mod, q="obscure")

    assert result["answered_by"] == "wikipedia_en_all_maxi_x"
    assert len(result["tried"]) == 2


def test_off_topic_results_fall_through_even_when_there_are_plenty(mod):
    """The case that broke on the real archives, reproduced with its real titles.

    Simple English had three hits for the Treaty of Nerchinsk, all of them
    year pages. By hit count that is coverage; by any reading of the titles
    it is not, and the full archive has the treaty.
    """
    mod._archives.update({
        "wikipedia_en_simple_all_x": StubArchive(["1680s", "1689", "1685"]),
        "wikipedia_en_all_maxi_x": StubArchive(["Treaty of Nerchinsk", "Nerchinsk", "Gantimur"]),
    })

    result = call(mod, q="What was the Treaty of Nerchinsk?")

    assert result["answered_by"] == "wikipedia_en_all_maxi_x"
    assert [r["title"] for r in result["results"]][0] == "Treaty of Nerchinsk"
    # Without saying why, a fallthrough looks the same as a broken archive.
    assert "none on topic" in result["tried"][0]


def test_one_shared_word_is_not_coverage(mod):
    """A generic article sharing one term must not stand in for the subject."""
    mod._archives.update({
        "wikipedia_en_simple_all_x": StubArchive(["Treaty", "Peace", "Russia"]),
        "wikipedia_en_all_maxi_x": StubArchive(["Treaty of Nerchinsk", "Nerchinsk"]),
    })

    result = call(mod, q="causes of the treaty of nerchinsk")

    assert result["answered_by"] == "wikipedia_en_all_maxi_x"


def test_question_words_do_not_count_as_matches(mod):
    """'What' and 'Explain' say how to answer, not what the answer is about."""
    assert mod._terms("Explain what the Kessler syndrome is") == {"kessler", "syndrome"}
    assert mod._on_topic("Explain what the Kessler syndrome is", ["Kessler Syndrome"])
    assert not mod._on_topic("Explain what the Kessler syndrome is", ["What", "Explanation"])


def test_the_index_is_searched_for_the_subject_not_the_question(mod):
    """Measured on the real archive: question words pulled in unrelated articles."""
    StubSearcher.queries.clear()
    mod._archives.update({"wikipedia_en_simple_all_x": StubArchive(["Ada Lovelace", "Lovelace"])})

    result = call(mod, q="Who was Ada Lovelace?")

    assert StubSearcher.queries == ["ada lovelace"]
    assert result["searched_for"] == "ada lovelace"
    assert result["query"] == "Who was Ada Lovelace?"


def test_a_question_of_only_stop_words_is_searched_as_written(mod):
    """An empty Xapian query matches nothing, which is worse than a vague one."""
    StubSearcher.queries.clear()
    mod._archives.update({"wikipedia_en_simple_all_x": StubArchive(["Who"])})

    call(mod, q="who was it")

    assert StubSearcher.queries == ["who was it"]


def test_titles_split_on_punctuation(mod):
    """Article titles use en dashes; a query typed on a phone does not."""
    assert mod._on_topic("zermelo fraenkel axiom of choice", ["Zermelo–Fraenkel set theory"])


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

    result = call(mod, q="a1", k=2, candidates=30)

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
