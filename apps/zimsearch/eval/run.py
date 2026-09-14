"""Score retrieval against real archives with a labelled question set.

Not part of CI: it needs the 127GB archive, which only exists on the Pi.
Run it there against one or more running zimsearch instances to compare
versions before merging a change to query handling or the cascade.

    python3 run.py http://127.0.0.1:8096=deployed http://127.0.0.1:8097=branch

Unit tests prove the logic does what it says. They cannot say whether that
logic finds the right article, which is the only thing that matters here,
and the one time that was checked by hand it did not.
"""
import json
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

QUESTIONS = json.loads((Path(__file__).parent / "questions.json").read_text(encoding="utf-8"))

# Things a person might type that should never produce a server error. The
# answer does not matter for these, only that the service stays up.
EDGE_CASES = [
    "?",
    "the the the",
    "who was it",
    "what is C++",
    "¿Quién fue Ada Lovelace?",
    "<script>alert(1)</script>",
    "' OR 1=1 --",
    "Ada Lovelace " * 40,
    "1969",
    "e = mc^2",
]


def norm(title: str) -> str:
    # Wikipedia titles use typographic dashes and apostrophes; questions
    # typed on a keyboard do not.
    t = unicodedata.normalize("NFKC", title).casefold()
    return t.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")


def search(base: str, q: str) -> tuple[int, dict, float]:
    url = f"{base}/search?" + urllib.parse.urlencode({"q": q, "k": 3, "chars": 100})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.status, json.load(r), time.perf_counter() - start
    except urllib.error.HTTPError as e:
        return e.code, {}, time.perf_counter() - start


def short(archive: str | None) -> str:
    if not archive:
        return "-"
    return "simple" if "simple" in archive else "full"


def evaluate(base: str) -> dict:
    rows, latencies = [], []
    for item in QUESTIONS:
        status, body, took = search(base, item["q"])
        latencies.append(took)
        titles = [r["title"] for r in body.get("results", [])]
        expected = {norm(e) for e in item["expect"]}
        rows.append({
            **item,
            "status": status,
            "top": titles,
            "archive": short(body.get("answered_by")),
            "at1": bool(titles) and norm(titles[0]) in expected,
            "at3": any(norm(t) in expected for t in titles),
        })
    edges = []
    for q in EDGE_CASES:
        status, body, took = search(base, q)
        latencies.append(took)
        edges.append((q, status, [r["title"] for r in body.get("results", [])][:2]))
    latencies.sort()
    return {
        "rows": rows,
        "edges": edges,
        "p50": latencies[len(latencies) // 2],
        "max": latencies[-1],
    }


def main() -> None:
    targets = [a.split("=", 1) if "=" in a else (a, a) for a in sys.argv[1:]]
    if not targets:
        sys.exit(__doc__)
    results = {name: evaluate(base) for base, name in targets}
    names = [n for _, n in targets]

    print(f"{'question':44} " + "  ".join(f"{n:>22}" for n in names))
    for i, item in enumerate(QUESTIONS):
        cells = []
        for n in names:
            r = results[n]["rows"][i]
            mark = "ok " if r["at1"] else ("top3" if r["at3"] else "MISS")
            cells.append(f"{mark:>4} {r['archive']:6} {(r['top'] or ['-'])[0][:10]:>10}")
        print(f"{item['q'][:44]:44} " + "  ".join(cells))

    print()
    kinds = sorted({q["kind"] for q in QUESTIONS})
    for n in names:
        rows = results[n]["rows"]
        parts = []
        for k in kinds + ["all"]:
            sel = [r for r in rows if k == "all" or r["kind"] == k]
            parts.append(f"{k} {sum(r['at1'] for r in sel)}/{len(sel)} @1, {sum(r['at3'] for r in sel)}/{len(sel)} @3")
        errors = [r for r in rows if r["status"] != 200] + [e for e in results[n]["edges"] if e[1] >= 500]
        print(f"{n}: " + " | ".join(parts))
        print(f"{'':{len(n) + 2}}latency p50 {results[n]['p50'] * 1000:.0f}ms, max {results[n]['max'] * 1000:.0f}ms, server errors {len(errors)}")

    print("\nedge cases (status, top results):")
    for i, q in enumerate(EDGE_CASES):
        cells = "  ".join(f"{n}: {results[n]['edges'][i][1]} {results[n]['edges'][i][2]}" for n in names)
        print(f"  {q[:30]!r:34} {cells}")



if __name__ == "__main__":
    main()
