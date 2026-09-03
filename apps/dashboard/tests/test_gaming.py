from unittest.mock import AsyncMock


def test_gaming_on_success(client, monkeypatch):
    from app import main

    fake_result = {"success": True, "output": "==> Done.", "exit_code": 0}
    monkeypatch.setattr(main, "run_gaming_script", AsyncMock(return_value=fake_result))

    res = client.post("/api/gaming/on")
    assert res.status_code == 200
    assert res.json() == fake_result
    main.run_gaming_script.assert_called_once_with("pregame.ps1", timeout=150)


def test_gaming_off_success(client, monkeypatch):
    from app import main

    fake_result = {"success": True, "output": "==> Done.", "exit_code": 0}
    monkeypatch.setattr(main, "run_gaming_script", AsyncMock(return_value=fake_result))

    res = client.post("/api/gaming/off")
    assert res.status_code == 200
    main.run_gaming_script.assert_called_once_with("postgame.ps1", timeout=210)


def test_gaming_on_reports_failure(client, monkeypatch):
    from app import main

    fake_result = {"success": False, "output": "drain did not complete cleanly"}
    monkeypatch.setattr(main, "run_gaming_script", AsyncMock(return_value=fake_result))

    res = client.post("/api/gaming/on")
    assert res.status_code == 200  # the HTTP call succeeded even though the script failed
    assert res.json()["success"] is False
