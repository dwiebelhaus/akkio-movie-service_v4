"""OpenAPI spec and docs UIs, served by hand so the spec can carry live data.

Genres come from imports, so they can't be a static enum. `/openapi.json` adds the current
genre names as an `enum` on the `genre` query parameter, giving Swagger UI a multi-select.
The spec is rebuilt only when the dataset version changes.
"""

import copy
import logging

from fastapi import APIRouter, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from app.dependencies import Catalog, Hub, Pool
from app.services.http_cache import dataset_version

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

OPENAPI_URL = "/openapi.json"
_GENRE_PATH = "/api/v1/movies"

_cache: dict[str, object] = {"version": None, "spec": None}


def with_genre_enum(spec: dict, genres: list[str]) -> dict:
    """A copy of `spec` with `genres` as the allowed values of the `genre` query parameter."""
    spec = copy.deepcopy(spec)
    for param in spec["paths"][_GENRE_PATH]["get"]["parameters"]:
        if param["name"] == "genre":
            param["schema"]["items"]["enum"] = genres
    return spec


@router.get(OPENAPI_URL)
async def openapi(request: Request, pool: Pool, hub: Hub, catalog: Catalog) -> JSONResponse:
    base = request.app.openapi()
    try:
        version = await dataset_version(hub, pool)
        if _cache["version"] != version:
            _cache["spec"] = with_genre_enum(base, await catalog.names(pool, version))
            _cache["version"] = version
        return JSONResponse(_cache["spec"])
    except Exception:
        # Docs must not depend on the database; fall back to the spec without the enum.
        logger.warning("serving OpenAPI spec without genre enum", exc_info=True)
        return JSONResponse(base)


@router.get("/docs")
async def swagger_ui(request: Request) -> HTMLResponse:
    return get_swagger_ui_html(openapi_url=OPENAPI_URL, title=f"{request.app.title} - Swagger UI")


@router.get("/redoc")
async def redoc(request: Request) -> HTMLResponse:
    return get_redoc_html(openapi_url=OPENAPI_URL, title=f"{request.app.title} - ReDoc")
