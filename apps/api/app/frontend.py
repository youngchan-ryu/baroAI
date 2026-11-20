"""Narrow same-origin frontend dispatch outside the protected API application."""

import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.responses import FileResponse, JSONResponse

ASSET = re.compile(r"index-[A-Za-z0-9_-]{8,}\.(?:js|css)")
NAVIGATION = re.compile(
    r"/(?:|login|staff-invite|report|me|w|work|work/[A-Za-z0-9-]+|"
    r"t/[A-Za-z0-9-]+|admin/(?:queue|settings|operations|export|t/[A-Za-z0-9-]+|"
    r"planning(?:/[A-Za-z0-9-]+(?:/insertions)?)?))"
)
STATIC = {"icon.svg", "icon-192.png", "icon-512.png", "manifest.webmanifest"}
MAP_FILES = {
    "NOTICE.txt": "text/plain",
    "gwanak.geojson": "application/geo+json",
    "yeongeon.geojson": "application/geo+json",
    "pyeongchang.geojson": "application/geo+json",
    "siheung.geojson": "application/geo+json",
    "suwon.geojson": "application/geo+json",
}


def validate_frontend(directory: Path) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("vercel_frontend_artifacts_missing")
    entries = {p.name for p in directory.iterdir()}
    if entries != STATIC | {"index.html", "assets", "maps"}:
        raise RuntimeError("vercel_frontend_artifacts_invalid")
    maps = directory / "maps"
    if (
        maps.is_symlink()
        or not maps.is_dir()
        or {p.name for p in maps.iterdir()} != MAP_FILES.keys()
        or any(p.is_symlink() or not p.is_file() for p in maps.iterdir())
    ):
        raise RuntimeError("vercel_frontend_artifacts_invalid")
    assets = directory / "assets"
    if assets.is_symlink() or not assets.is_dir():
        raise RuntimeError("vercel_frontend_artifacts_invalid")
    files = list(assets.iterdir())
    if (
        not files
        or not any(p.suffix == ".js" for p in files)
        or not any(p.suffix == ".css" for p in files)
    ):
        raise RuntimeError("vercel_frontend_artifacts_invalid")
    if any(p.is_symlink() or not p.is_file() or not ASSET.fullmatch(p.name) for p in files):
        raise RuntimeError("vercel_frontend_artifacts_invalid")
    html = directory / "index.html"
    if not html.is_symlink() and html.is_file():
        references = set(re.findall(r"/assets/([^\"\']+)", html.read_text(encoding="utf-8")))
        if references != {p.name for p in files}:
            raise RuntimeError("vercel_frontend_artifacts_invalid")
    if any(
        (directory / name).is_symlink() or not (directory / name).is_file()
        for name in STATIC | {"index.html"}
    ):
        raise RuntimeError("vercel_frontend_artifacts_invalid")


class FrontendDispatcher:
    def __init__(self, api: FastAPI, directory: Path):
        self.api = api
        self.directory = directory
        self.validated = False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.api(scope, receive, send)
            return
        if not self.validated:
            validate_frontend(self.directory)
            self.validated = True
        path = scope["path"]
        raw = scope.get("raw_path", b"")
        # Reject encoded and noncanonical paths before any frontend or API routing.
        if (
            scope.get("root_path", "") != ""
            or raw != path.encode("utf-8")
            or "\\" in path
            or "//" in path
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            response = JSONResponse(
                {"error": {"code": "not_found"}},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        if path == "/api" or path.startswith("/api/"):
            scope = dict(scope, path=path[4:] or "/", raw_path=raw[4:] or b"/", root_path="")
            await self.api(scope, receive, send)
            return
        relative = path.removeprefix("/")
        file = None
        cache = "no-store"
        media_type = None
        if scope["method"] in {"GET", "HEAD"}:
            if NAVIGATION.fullmatch(path):
                file = self.directory / "index.html"
            elif relative in STATIC:
                file = self.directory / relative
            elif relative.startswith("maps/") and relative[5:] in MAP_FILES:
                file = self.directory / relative
                media_type = MAP_FILES[relative[5:]]
                cache = "public, max-age=3600"
            elif relative.startswith("assets/") and ASSET.fullmatch(relative[7:]):
                file = self.directory / relative
                cache = "public, max-age=31536000, immutable"
        if (
            file is not None
            and not self.directory.is_symlink()
            and not file.parent.is_symlink()
            and file.is_file()
            and not file.is_symlink()
        ):
            await FileResponse(file, media_type=media_type, headers={"Cache-Control": cache})(
                scope, receive, send
            )
        else:
            await self.api(scope, receive, send)


def create_frontend_app(api: FastAPI, directory: Path) -> FastAPI:
    dispatcher = FrontendDispatcher(api, directory)

    @asynccontextmanager
    async def lifespan(app):
        validate_frontend(directory)
        dispatcher.validated = True
        async with api.router.lifespan_context(api):
            yield

    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan, redirect_slashes=False
    )
    app.state = api.state
    app.mount("/", dispatcher)
    return app
