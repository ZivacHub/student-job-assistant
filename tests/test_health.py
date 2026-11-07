from fastapi.testclient import TestClient

from app.main import app, create_app


def test_health_ok_and_dry_run_default_on():
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "dry_run": True}


def test_api_docs_are_hidden_by_default():
    with TestClient(app) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404, path


def test_api_docs_are_available_in_debug_mode():
    with TestClient(create_app(debug=True)) as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200
