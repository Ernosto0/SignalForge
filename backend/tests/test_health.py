from fastapi.testclient import TestClient

from signalforge.api.main import app


def test_health_reports_status() -> None:
    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert isinstance(body["database"], bool)
