"""Wikipedia lookup for conversations, against the local ZIM archive.

The model this runs on is a 7B at Q4. It knows a surprising amount and
states all of it with the same confidence, which is the problem: there is
no way to tell from the reply whether an answer came from something it
learned or something it assembled. Retrieval does not make the model
smarter, it makes the answer checkable - every passage carries a link to
the article it came from on wikipedia.home.

Opt-in per message rather than always on. Most turns in a conversation are
not factual lookups, and searching for "can you rewrite that shorter"
returns articles about rewriting and shortness, which then occupy the
context the actual conversation needs.
"""
import httpx

from app.config import RETRIEVAL_CHARS, RETRIEVAL_RESULTS, ZIMSEARCH_URL
from app.logging import get_logger

log = get_logger(__name__)

# Connect fails fast because this is one hop inside the cluster; if the pod
# is not there, waiting longer will not produce it. Read is looser than the
# measured 30ms search because the first query after a pod start pays a
# cold mmap of a 127GB file off the Pi's disk.
TIMEOUT = httpx.Timeout(connect=2.0, read=15.0, write=5.0, pool=2.0)


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=ZIMSEARCH_URL, timeout=TIMEOUT)


async def lookup(http: httpx.AsyncClient, question: str) -> list[dict]:
    """Return passages for a question, or nothing at all.

    Never raises. Retrieval is an enhancement to a turn that can proceed
    without it, so a missing archive or a stopped pod costs the user their
    citations, not their answer. The distinction is visible either way: the
    reply carries sources or it does not.

    The question is sent as written. Xapian wants terms and a follow-up
    like "and what about his brother" carries almost none, so retrieval is
    weakest exactly where a conversation is most conversational. Rewriting
    the query with the model first would fix that and cost a second round
    trip before the first token - not obviously worth it, and easy to add
    later if the failure shows up in practice.
    """
    try:
        response = await http.get(
            "/search",
            params={"q": question, "k": RETRIEVAL_RESULTS, "chars": RETRIEVAL_CHARS},
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        log.warning("retrieval_failed", error=f"{type(exc).__name__}: {exc}")
        return []
    results = body.get("results") or []
    log.info("retrieval_done", answered_by=body.get("answered_by"), hits=len(results))
    return results


def as_context(results: list[dict]) -> str:
    """Format passages as the system message that precedes the question.

    The instruction not to invent a citation matters more than the passages
    themselves. A small model shown the shape of a sourced answer will
    happily produce that shape for claims the passages never made, and a
    fabricated citation is worse than none - it looks like the checkable
    answer this whole feature exists to provide.
    """
    passages = "\n\n".join(
        f"[{n}] {r['title']} ({r['url']})\n{r['text']}"
        for n, r in enumerate(results, 1)
    )
    return (
        "Excerpts from a local copy of Wikipedia, retrieved for the question "
        "below. Use them where they are relevant, and mark each claim you take "
        "from one with its number in square brackets, like [1]. Where they do "
        "not cover the question, answer from your own knowledge and say so "
        "rather than reaching for a passage. Never put a number on a sentence "
        "the passages do not support."
        + "\n\n"
        + passages
    )


# How much of each passage is kept with the answer. Enough to see why the
# model said what it said, not so much that the stored conversation becomes
# a copy of the encyclopedia - the whole article is one click away anyway.
STORED_CHARS = 400


def as_stored(results: list[dict]) -> list[dict]:
    """The record of what an answer was given, kept beside it.

    Stored rather than appended to the reply text. A text footer replayed
    into the model's context on every following turn, and could not hold
    the passage itself - only which article it came from, which was the
    part that was never in doubt.
    """
    return [
        {
            "n": n,
            "title": r["title"],
            "archive": r["archive"],
            "url": r["url"],
            # Trimmed on a word boundary, because a passage cut mid-word
            # reads as a rendering bug rather than a deliberate excerpt.
            "excerpt": _trim(r["text"], STORED_CHARS),
        }
        for n, r in enumerate(results, 1)
    ]


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut) + "..."
