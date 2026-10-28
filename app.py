from api.index import app
from pathlib import Path
from starlette.responses import JSONResponse
from starlette.routing import Match

_api_routes = tuple(app.router.routes)


def _matches_api(scope):
    return any(route.matches(scope)[0] is not Match.NONE for route in _api_routes)


@app.middleware("http")
async def _preserve_api_not_found(request, call_next):
    path = request.scope["path"]
    if (path == "/api" or path.startswith("/api/")) and not _matches_api(request.scope):
        redirected_scope = dict(request.scope, path=path.rstrip("/") if path.endswith("/") else path + "/")
        if not (app.router.redirect_slashes and _matches_api(redirected_scope)):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await call_next(request)


# Vercel serves public assets from the CDN and can omit them from the function.
_frontend_directory = Path(__file__).resolve().parent / "public"
if _frontend_directory.is_dir():
    app.frontend("/", directory=_frontend_directory, fallback="index.html")
