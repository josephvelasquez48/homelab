"""Tests for the GPU release endpoint.

This replaced an SSH-and-PowerShell path that cordoned and drained a K3s
node. The authorization tests below are carried over unchanged in intent:
every one asserts the action was never *awaited*, not merely that the
status was 4xx. A 401 that still evicted the model would satisfy a
status-code-only assertion while doing the thing it refused.
"""
from unittest.mock import AsyncMock

import pytest

from app import auth, gpu, main


def test_release_evicts_loaded_models(authed_client, monkeypatch):
    result = {
        "success": True,
        "evicted": ["qwen2.5-coder:7b"],
        "still_loaded": [],
        "vram_freed_bytes": 4748056984,
        "errors": [],
    }
    monkeypatch.setattr(gpu, "release", AsyncMock(return_value=result))

    res = authed_client.post("/api/gpu/release", json={})
    assert res.status_code == 200
    assert res.json() == result
    gpu.release.assert_awaited_once()


def test_release_with_nothing_loaded_is_still_success(authed_client, monkeypatch):
    """An idle GPU is the goal state, not a failure - Ollama evicts on its
    own after a timeout, so pressing the button twice is normal."""
    monkeypatch.setattr(gpu, "release", AsyncMock(return_value={
        "success": True, "evicted": [], "still_loaded": [],
        "vram_freed_bytes": 0, "errors": [],
    }))

    res = authed_client.post("/api/gpu/release", json={})
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert res.json()["evicted"] == []


def test_release_reports_a_model_that_would_not_evict(authed_client, monkeypatch):
    """Still HTTP 200 - the call worked, the eviction did not. Those are
    different failures and the page needs to tell them apart."""
    monkeypatch.setattr(gpu, "release", AsyncMock(return_value={
        "success": False, "evicted": [], "still_loaded": ["qwen2.5-coder:7b"],
        "vram_freed_bytes": 0, "errors": ["qwen2.5-coder:7b: TimeoutException: "],
    }))

    res = authed_client.post("/api/gpu/release", json={})
    assert res.status_code == 200
    assert res.json()["success"] is False
    assert res.json()["still_loaded"] == ["qwen2.5-coder:7b"]


def test_unreachable_ollama_is_502_not_500(authed_client, monkeypatch):
    monkeypatch.setattr(gpu, "release", AsyncMock(side_effect=OSError("connection refused")))

    res = authed_client.post("/api/gpu/release", json={})
    assert res.status_code == 502
    assert "could not reach Ollama" in res.json()["detail"]


# --- authorization: the boundary -------------------------------------------


def test_release_requires_authentication(client, monkeypatch):
    monkeypatch.setattr(gpu, "release", AsyncMock())

    res = client.post("/api/gpu/release", json={})
    assert res.status_code == 401
    gpu.release.assert_not_awaited()


def test_bodyless_cors_simple_post_is_rejected(client, monkeypatch):
    """The exact request a hostile third-party page can send.

    fetch(url, {method: 'POST', mode: 'no-cors'}) sends no Content-Type,
    and under SameSite=Strict carries no cookie, so it must fail on
    authorization before content type is ever considered.
    """
    monkeypatch.setattr(gpu, "release", AsyncMock())

    res = client.post("/api/gpu/release")
    assert res.status_code == 401
    gpu.release.assert_not_awaited()


def test_authorization_is_checked_before_content_type(client, monkeypatch):
    """Unauthenticated plus wrong content type is 401, not 415.

    Ordering decides what a failure tells an attacker: 415 would confirm
    the endpoint is reachable and imply the content-type rule is what
    stands in the way. It isn't - authorization is.
    """
    monkeypatch.setattr(gpu, "release", AsyncMock())

    res = client.post("/api/gpu/release", data="password=x")
    assert res.status_code == 401
    gpu.release.assert_not_awaited()


def test_authenticated_form_post_is_rejected(authed_client, monkeypatch):
    """Defence in depth: even a valid session cannot use a simple request."""
    monkeypatch.setattr(gpu, "release", AsyncMock())

    res = authed_client.post(
        "/api/gpu/release",
        data="x=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert res.status_code == 415
    gpu.release.assert_not_awaited()


def test_logout_revokes_access(authed_client, monkeypatch):
    monkeypatch.setattr(gpu, "release", AsyncMock())

    assert authed_client.post("/api/logout", json={}).status_code == 200

    res = authed_client.post("/api/gpu/release", json={})
    assert res.status_code == 401
    gpu.release.assert_not_awaited()


def test_forged_session_cookie_is_rejected(client, monkeypatch):
    """The cookie is signed, so a hand-written one cannot authenticate."""
    monkeypatch.setattr(gpu, "release", AsyncMock())

    client.cookies.set("dashboard_session", "eyJhdXRoZW50aWNhdGVkIjogdHJ1ZX0=")
    res = client.post("/api/gpu/release", json={})
    assert res.status_code == 401
    gpu.release.assert_not_awaited()


def test_unconfigured_password_fails_closed(client, monkeypatch):
    """No dashboard-auth Secret must mean unreachable, never open."""
    monkeypatch.setattr(auth, "DASHBOARD_PASSWORD", "")
    monkeypatch.setattr(gpu, "release", AsyncMock())

    assert client.post("/api/login", json={"password": ""}).status_code == 401
    assert client.post("/api/login", json={"password": "anything"}).status_code == 401
    assert client.post("/api/gpu/release", json={}).status_code == 401
    gpu.release.assert_not_awaited()


def test_unconfigured_password_issues_no_cookie(client, monkeypatch):
    """A rejected login must not hand out a session anyway."""
    monkeypatch.setattr(auth, "DASHBOARD_PASSWORD", "")

    res = client.post("/api/login", json={"password": "anything"})
    assert res.status_code == 401
    assert "set-cookie" not in {k.lower() for k in res.headers}


def test_session_cookie_attributes(client, password):
    """Pins the attributes the cross-site defence depends on.

    SameSite=Strict is what withholds the cookie from a third-party
    page's request; HttpOnly is what keeps the credential out of the
    JavaScript that anyone able to load the dashboard can read. Flipping
    either back - to Lax, or by dropping HttpOnly - would reopen the
    browser-driven path without changing a single status code, so assert
    on them directly rather than trusting review to catch it.
    """
    res = client.post("/api/login", json={"password": password})
    assert res.status_code == 200

    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "dashboard_session=" in cookie
