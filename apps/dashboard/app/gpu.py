"""Frees the GPU by evicting Ollama's resident models, over Ollama's own API.

This replaces an SSH-and-PowerShell path that cordoned and drained the
desktop K3s node. That node no longer exists - the worker moved to an M1
MacBook and the desktop left the cluster entirely
(docs/node-migration.md) - so there is nothing to cordon, and the CPU
contention that justified draining went with it.

What remains is narrower and honest: Ollama holds ~4.7GB in VRAM and a
game wants it back. Ollama already evicts after an idle period, so this
only makes it immediate rather than eventual.

Going through Ollama's HTTP API rather than SSH removes a mounted private
key, known_hosts handling, and remote PowerShell execution from a pod -
which docs/security-testing.md called the highest-impact surface in this
project. The dashboard already reaches the inference Service in-cluster,
so this needs no new access of any kind.
"""
import httpx

from app.config import INFERENCE_URL


async def loaded_models(client: httpx.AsyncClient) -> list[dict]:
    """Models currently resident in VRAM. Empty list means the GPU is free."""
    r = await client.get(f"{INFERENCE_URL}/api/ps", timeout=10.0)
    r.raise_for_status()
    return [
        {"name": m.get("name"), "size_vram": m.get("size_vram") or 0}
        for m in (r.json().get("models") or [])
    ]


async def release(client: httpx.AsyncClient) -> dict:
    """Evict every resident model, then report what actually changed.

    `keep_alive: 0` with no `prompt` is Ollama's documented eviction call -
    it returns `done_reason: unload` and generates nothing, confirmed
    against v0.32.5 before this was written. Sending a prompt would work
    too but would burn GPU time producing a token nobody reads.

    Issued per model because Ollama has no evict-everything call and more
    than one model can be resident at once.

    There is deliberately no matching "on" action. Nothing needs turning
    back on: the next inference request reloads on demand. That is what
    collapsed the old pregame/postgame pair into a single button.
    """
    before = await loaded_models(client)
    errors = []
    for m in before:
        try:
            resp = await client.post(
                f"{INFERENCE_URL}/api/generate",
                json={"model": m["name"], "keep_alive": 0},
                timeout=60.0,
            )
            resp.raise_for_status()
        except Exception as exc:
            errors.append(f"{m['name']}: {type(exc).__name__}: {exc}")

    after = await loaded_models(client)
    freed = sum(m["size_vram"] for m in before) - sum(m["size_vram"] for m in after)
    return {
        # success is measured by what is no longer resident, not by the
        # eviction calls returning 200 - the point is the VRAM, not the API.
        "success": not after and not errors,
        "evicted": [m["name"] for m in before if m["name"] not in {a["name"] for a in after}],
        "still_loaded": [m["name"] for m in after],
        "vram_freed_bytes": freed,
        "errors": errors,
    }
