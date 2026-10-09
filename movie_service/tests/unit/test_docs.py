from app.main import app
from app.routers.docs import with_genre_enum


def _genre_param(spec: dict) -> dict:
    params = spec["paths"]["/api/v1/movies"]["get"]["parameters"]
    return next(p for p in params if p["name"] == "genre")


def test_genre_enum_added_without_mutating_base_spec():
    base = app.openapi()
    spec = with_genre_enum(base, ["Action", "Drama"])
    assert _genre_param(spec)["schema"]["items"]["enum"] == ["Action", "Drama"]
    assert "enum" not in _genre_param(base)["schema"]["items"]
