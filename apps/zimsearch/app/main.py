"""JSON full-text search over local ZIM archives, for LLM retrieval.

Exists because the chat assistant needs passages, and neither obvious
alternative works here.

Embedding Wikipedia into pgvector is not feasible: the full archive is
millions of articles, and generating that many embeddings on one consumer
GPU would take weeks and produce a vector store larger than the source.
The archive already ships its own Xapian full-text index, so the retrieval
problem is solved before it starts.

Scraping kiwix-serve's /search costs an HTTP hop and an RSS parse to get a
worse result - a fixed ~500 character snippet rather than the article text,
with no control over how much is returned.

So this reads the archive directly through libzim. Measured on the Pi:
archive opens in 40ms, search returns in 30ms, entry fetch in 1ms.

It is a separate service rather than part of the api for one reason: the
archive is a 127GB hostPath on a single node. Putting libzim in the api
would pin the api to that node and cost the second replica. Here, only this
service is pinned, and the api reaches it over the cluster network.
"""
import html
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from libzim.reader import Archive
from libzim.search import Query as ZimQuery
from libzim.search import Searcher

ZIM_DIR = Path(os.environ.get("ZIM_DIR", "/data"))

# Characters of article text returned per hit by default. The assistant runs
# a 7B model with a modest context window, so retrieval that returns whole
# articles would push the actual conversation out of the window - the model
# would answer from the article and forget what was asked.
DEFAULT_CHARS = int(os.environ.get("SNIPPET_CHARS", "1200"))

# Archives are tried in this order, not merged. Simple English says the same
# thing in a fraction of the tokens, which matters on a 7B model, but it is
# a far smaller encyclopedia - roughly 400k articles against millions. So it
# answers first when it can, and the full archive covers what it cannot.
#
# Matching is by prefix so the dated filenames do not need updating here.
ARCHIVE_ORDER = [
    a.strip()
    for a in os.environ.get(
        "ARCHIVE_ORDER", "wikipedia_en_simple_all,wikipedia_en_all_maxi"
    ).split(",")
    if a.strip()
]

# Below this many hits, the concise archive is treated as not really having
# the topic and the query falls through.
MIN_HITS = int(os.environ.get("MIN_HITS", "2"))

# Words that say what kind of answer someone wants rather than what it is
# about. Left in, they steer Xapian toward whatever article uses them most:
# measured on the real Simple English archive, "who was Ada Lovelace"
# returned Federico Menabrea, Charles Babbage and Nottingham, while "ada
# lovelace" returned Ada Lovelace. "explain the Kessler syndrome" returned
# lists of deaths. "work" and "important" are here for the same reason -
# "how does photosynthesis work" found biochemists, not photosynthesis.
_STOP = frozenset(
    "a an and are as at be by can did do does explain describe for from how i "
    "in is it me of on or tell the this to was were what when where which who "
    "why with you about work works important mean means meaning happened".split()
)
_WORD = re.compile(r"\w+")

app = FastAPI(title="ZIM Search")

_archives: dict[str, Archive] = {}


def _load() -> dict[str, Archive]:
    """Open every archive once and keep it. Opening is cheap, repeating it isn't."""
    if not _archives:
        for path in sorted(ZIM_DIR.glob("*.zim")):
            # aria2 leaves a .aria2 control file beside an incomplete
            # download. Opening a partial 127GB archive can succeed and then
            # return wrong results, which is worse than refusing it.
            if path.with_suffix(path.suffix + ".aria2").exists():
                continue
            try:
                _archives[path.stem] = Archive(str(path))
            except Exception:
                continue
    return _archives


def _keywords(text: str) -> list[str]:
    """The words of a question that name its subject, in the order written."""
    seen: dict[str, None] = {}
    for w in _WORD.findall(text.lower()):
        if len(w) > 1 and w not in _STOP:
            seen.setdefault(w)
    return list(seen)


# A question longer than this many subject words is searched for its first
# ones. Someone pasting a paragraph and asking about it names the subject
# early, and a Xapian query of two hundred terms matches everything weakly.
MAX_TERMS = 12


def _stem(word: str) -> str:
    """Just enough to match "vaccines" to the article "Vaccine".

    Not a stemmer - Xapian stems its own queries. This is only for comparing
    a question's words with titles, which are almost always singular. Found
    by the evaluation: "how do vaccines work" judged every Simple English
    result off topic and fell through to a worse answer from the full archive.
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _terms(text: str) -> set[str]:
    return {_stem(w) for w in _keywords(text)[:MAX_TERMS]}


def _on_topic(question: str, titles: list[str]) -> bool:
    """Whether the top results are about the question, judged by their titles.

    Hit count alone does not work, which was found on the real archives
    rather than in tests. Xapian matches loosely enough that almost any
    question finds two or more articles, so the concise archive always
    "covered" the topic and the full one was never consulted. For "Treaty of
    Nerchinsk", Simple English returned the articles 1680s, 1689 and 1685 -
    pages that mention the year it was signed - and those were handed to the
    model while the full archive had the treaty itself.

    Titles rather than article text because every result matched the text;
    that is why it was returned. A title naming the subject is the signal
    Xapian's own ranking does not give. Two matching terms, or half of them,
    counts as on topic: one term out of four is how "Treaty" alone would
    claim to cover "the Treaty of Nerchinsk".
    """
    wanted = _terms(question)
    if not wanted:
        return True
    seen = set().union(*(_terms(t) for t in titles)) if titles else set()
    found = wanted & seen
    return len(found) >= 2 or len(found) * 2 >= len(wanted)


def _ordered(archives: dict[str, Archive]) -> list[tuple[str, Archive]]:
    """Archives in configured preference order, unlisted ones last."""
    ranked = []
    for prefix in ARCHIVE_ORDER:
        ranked += [(n, a) for n, a in sorted(archives.items()) if n.startswith(prefix)]
    ranked += [(n, a) for n, a in sorted(archives.items()) if (n, a) not in ranked]
    return ranked


_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def _to_text(raw: bytes) -> str:
    """Strip an article's HTML to plain prose.

    Deliberately crude. The consumer is a language model, which does not
    need structure preserved, and a real HTML parser would add a dependency
    and latency to produce something no more useful here.
    """
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"(?is)<(script|style|table)\b.*?</\1>", " ", text)
    return _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", text))).strip()


@app.get("/health")
async def health() -> dict:
    archives = _load()
    return {"status": "ok", "archives": sorted(archives)}


@app.get("/search")
async def search(
    # As long as a chat message may be. A lower cap here turned long
    # questions into a 422 that the api, correctly, treats as "no sources" -
    # so the lookup failed silently for exactly the questions with the most
    # context in them.
    q: str = Query(min_length=1, max_length=32000),
    k: int = Query(3, ge=1, le=10),
    chars: int = Query(DEFAULT_CHARS, ge=100, le=8000),
    book: str | None = None,
    candidates: int = Query(0, ge=0, le=50),
) -> dict:
    """Search archives in preference order, stopping at the first good answer.

    Not a merge. The concise archive answers when it has the topic, because
    its articles cost a fraction of the context; the full archive covers what
    it does not, which is most of the long tail. Merging both would spend the
    budget saying the same thing twice.

    `candidates` over-fetches without widening what is returned. It exists
    for a reranking step that does not yet exist: fetch 30, score them with
    a small embedding model, keep the best k. Xapian ranks on term
    statistics and has no idea what the question means, so its ordering is a
    reasonable shortlist and a poor final answer. Nothing reranks today, so
    the extra candidates are simply trimmed - the parameter is a seam, not a
    feature.
    """
    archives = _load()
    if not archives:
        raise HTTPException(status_code=503, detail=f"no ZIM archives in {ZIM_DIR}")
    if book and book not in archives:
        raise HTTPException(status_code=404, detail=f"no such archive: {book}")

    order = [(book, archives[book])] if book else _ordered(archives)
    fetch = max(k, candidates)
    tried: list[str] = []
    # The chat sends the question as typed. Xapian gets its subject words;
    # a question that is nothing but stop words is searched as written
    # rather than as an empty query.
    xapian_q = " ".join(_keywords(q)[:MAX_TERMS]) or q[:200]

    for index, (name, archive) in enumerate(order):
        last = index == len(order) - 1
        try:
            result = Searcher(archive).search(ZimQuery().set_query(xapian_q))
            paths = list(result.getResults(0, fetch))
        except Exception as exc:
            tried.append(f"{name} (error: {type(exc).__name__})")
            continue

        tried.append(f"{name} ({len(paths)} hits)")
        # Too few hits means this archive does not really cover the topic.
        # On the last archive there is nowhere left to fall through to, so
        # thin results beat none.
        if len(paths) < MIN_HITS and not last:
            continue

        entries = []
        for path in paths:
            try:
                entries.append((path, archive.get_entry_by_path(path)))
            except Exception:
                continue
        # Judged on what would be returned, not on the whole candidate list:
        # an on-topic article at position 30 does not help an answer built
        # from the first three.
        if not last and not _on_topic(q, [e.title for _, e in entries[:k]]):
            tried[-1] = f"{name} ({len(paths)} hits, none on topic)"
            continue

        hits = []
        for path, entry in entries:
            try:
                text = _to_text(bytes(entry.get_item().content))
            except Exception:
                continue
            hits.append({
                "archive": name,
                "title": entry.title,
                "path": path,
                # Where a person can read the whole thing, so an answer can
                # be checked against its source rather than trusted.
                "url": f"https://wikipedia.home/content/{name}/{path}",
                "text": text[:chars],
                "truncated": len(text) > chars,
            })
        if not hits:
            continue

        return {
            "query": q,
            "searched_for": xapian_q,
            "answered_by": name,
            # Which archives were consulted and what each returned. Without
            # this a thin answer is indistinguishable from a broken cascade.
            "tried": tried,
            "reranked": False,
            "results": hits[:k],
        }

    return {
        "query": q,
        "searched_for": xapian_q,
        "answered_by": None,
        "tried": tried,
        "reranked": False,
        "results": [],
    }
