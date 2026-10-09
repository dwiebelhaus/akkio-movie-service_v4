from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import ApiError, register_error_handlers


def make_client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret internals")

    @app.get("/conflict")
    async def conflict():
        raise ApiError(409, "job_not_ready", "Job is still running", {"status": "running"})

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_exception_is_generic_500():
    resp = make_client().get("/boom")
    assert resp.status_code == 500
    assert resp.json() == {
        "error": {"code": "internal_error", "message": "Internal server error", "details": None}
    }
    assert "secret" not in resp.text


def test_api_error_maps_status_and_code():
    resp = make_client().get("/conflict")
    assert resp.status_code == 409
    assert resp.json()["error"] == {
        "code": "job_not_ready",
        "message": "Job is still running",
        "details": {"status": "running"},
    }


def test_validation_error_is_422_with_details():
    resp = make_client().get("/items/abc")
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "validation_error"
    assert body["details"][0]["loc"] == ["path", "item_id"]
