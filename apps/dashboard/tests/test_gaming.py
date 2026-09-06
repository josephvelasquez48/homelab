from unittest.mock import AsyncMock


def test_gaming_on_success(authed_client, monkeypatch):
    from app import main

    fake_result = {"success": True, "output": "==> Done.", "exit_code": 0}
    monkeypatch.setattr(main, "run_gaming_script", AsyncMock(return_value=fake_result))

    res = authed_client.post("/api/gaming/on", json={})
    assert res.status_code == 200
    assert res.json() == fake_result
    main.run_gaming_script.assert_called_once_with("pregame.ps1", timeout=150)


def test_gaming_off_success(authed_client, monkeypatch):
    from app import main

    fake_result = {"success": True, "output": "==> Done.", "exit_code": 0}
    monkeypatch.setattr(main, "run_gaming_script", AsyncMock(return_value=fake_result))

    res = authed_client.post("/api/gaming/off", json={})
    assert res.status_code == 200
    main.run_gaming_script.assert_called_once_with("postgame.ps1", timeout=210)


def test_gaming_on_reports_failure(authed_client, monkeypatch):
    from app import main

    fake_result = {"success": False, "output": "drain did not complete cleanly"}
    monkeypatch.setattr(main, "run_gaming_script", AsyncMock(return_value=fake_result))

    res = authed_client.post("/api/gaming/on", json={})
    assert res.status_code == 200  # the HTTP call succeeded even though the script failed
    assert res.json()["success"] is False


# --- authorization: the boundary -------------------------------------------
#
# Every case below asserts run_gaming_script was never awaited, not just
# that the status was 4xx. A 401 that still SSHes to the desktop and drains
# a node would satisfy a status-code-only assertion.


def test_gaming_requires_authentication(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "run_gaming_script", AsyncMock())

    for path in ("/api/gaming/on", "/api/gaming/off"):
        res = client.post(path, json={})
        assert res.status_code == 401, path
    main.run_gaming_script.assert_not_awaited()


def test_bodyless_cors_simple_post_is_rejected(client, monkeypatch):
    """The exact request a hostile third-party page can send.

    fetch(url, {method: 'POST', mode: 'no-cors'}) sends no Content-Type,
    and under SameSite=Strict carries no cookie, so it must fail on
    authorization before content type is ever considered.
    """
    from app import main

    monkeypatch.setattr(main, "run_gaming_script", AsyncMock())

    res = client.post("/api/gaming/on")
    assert res.status_code == 401
    main.run_gaming_script.assert_not_awaited()


def test_authorization_is_checked_before_content_type(client, monkeypatch):
    """Unauthenticated plus wrong content type is 401, not 415.

    Ordering decides what a failure tells an attacker: 415 would confirm
    the endpoint is reachable and imply the content-type rule is what
    stands in the way. It isn't - authorization is.
    """
    from app import main

    monkeypatch.setattr(main, "run_gaming_script", AsyncMock())

    res = client.post("/api/gaming/on", data="password=x")
    assert res.status_code == 401
    main.run_gaming_script.assert_not_awaited()


def test_authenticated_form_post_is_rejected(authed_client, monkeypatch):
    """Defence in depth: even a valid session cannot use a simple request."""
    from app import main

    monkeypatch.setattr(main, "run_gaming_script", AsyncMock())

    res = authed_client.post(
        "/api/gaming/on",
        data="x=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert res.status_code == 415
    main.run_gaming_script.assert_not_awaited()


def test_logout_revokes_access(authed_client, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "run_gaming_script", AsyncMock())

    assert authed_client.post("/api/logout", json={}).status_code == 200

    res = authed_client.post("/api/gaming/on", json={})
    assert res.status_code == 401
    main.run_gaming_script.assert_not_awaited()


def test_forged_session_cookie_is_rejected(client, monkeypatch):
    """The cookie is signed, so a hand-written one cannot authenticate."""
    from app import main

    monkeypatch.setattr(main, "run_gaming_script", AsyncMock())

    client.cookies.set("dashboard_session", "eyJhdXRoZW50aWNhdGVkIjogdHJ1ZX0=")
    res = client.post("/api/gaming/on", json={})
    assert res.status_code == 401
    main.run_gaming_script.assert_not_awaited()


def test_unconfigured_password_fails_closed(client, monkeypatch):
    """No dashboard-auth Secret must mean unreachable, never open."""
    from app import auth, main

    monkeypatch.setattr(auth, "DASHBOARD_PASSWORD", "")
    monkeypatch.setattr(main, "run_gaming_script", AsyncMock())

    assert (
        client.post("/api/login", json={"password": ""}).status_code == 401
    )
    assert (
        client.post(
            "/api/login", json={"password": "anything"}
        ).status_code
        == 401
    )
    assert client.post("/api/gaming/on", json={}).status_code == 401
    main.run_gaming_script.assert_not_awaited()


def test_unconfigured_password_issues_no_cookie(client, monkeypatch):
    """A rejected login must not hand out a session anyway."""
    from app import auth

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
