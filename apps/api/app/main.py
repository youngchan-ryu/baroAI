import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from app.ai_followup_routes import router as ai_followup_router
from app.ai_metrics_routes import router as ai_metrics_router
from app.auth_routes import router as auth_router
from app.config import Settings
from app.db import Database
from app.dispatch_routes import router as dispatch_router
from app.duplicate_routes import router as duplicate_router
from app.execution_routes import router as execution_router
from app.information_requests import consent_router as information_consent_router
from app.information_requests import router as information_requests_router
from app.insertion_routes import router as insertion_router
from app.mail_recovery import router as mail_recovery_router
from app.middleware import UploadBodyLimit
from app.operations_metrics_routes import router as operations_metrics_router
from app.routes import router
from app.school_catalog_routes import router as school_catalog_router
from app.school_workers_routes import router as school_workers_router
from app.security import service_host_allowed
from app.services.llm import runtime_mode
from app.staff_auth import router as staff_auth_router

logger = logging.getLogger("baroai.http")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["baroai-api"] = "baroai-api"
    environment: Literal["dev", "test", "prod"]
    llm_mode: Literal["replay", "live", "unavailable"]


class ReadyResponse(BaseModel):
    status: Literal["ready"] = "ready"
    database: Literal["ok"] = "ok"


class NotReadyResponse(BaseModel):
    status: Literal["not_ready"] = "not_ready"
    database: Literal["unavailable"] = "unavailable"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    database = Database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.deployment_mode in {"service", "prototype", "public_demo"}:
            from app.auth_service import ensure_namespace
            from app.dispatch_lock import dispatch_lock
            from app.planning.datasets import DatasetProvider
            from app.planning.registry import active_generation

            if settings.planning_dataset_path or settings.planning_dataset_sha256:
                app.state.planning_dataset = DatasetProvider.from_startup_file(
                    settings.planning_dataset_path, settings.planning_dataset_sha256
                )

            async with database.transaction() as db:
                await dispatch_lock(db)
                await ensure_namespace(db, settings)
                if app.state.planning_dataset is not None:
                    app.state.planning_dataset_revision = await active_generation(
                        db, settings, app.state.planning_dataset
                    )
        if settings.deployment_mode == "service":
            from app.photo_coordinates import purge_photo_coordinates

            try:
                await purge_photo_coordinates(database)
            except Exception:
                await database.close()
                raise RuntimeError("Photo coordinate cleanup failed at startup") from None
        try:
            yield
        finally:
            await database.close()

    app = FastAPI(title="바로AI API", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    app.include_router(mail_recovery_router)
    app.include_router(auth_router)
    app.include_router(staff_auth_router)
    app.include_router(ai_metrics_router)
    app.include_router(operations_metrics_router)
    app.include_router(duplicate_router)
    app.include_router(dispatch_router)
    app.include_router(execution_router)
    app.include_router(insertion_router)
    app.include_router(ai_followup_router)
    app.include_router(information_requests_router)
    app.include_router(information_consent_router)
    app.include_router(school_catalog_router)
    app.include_router(school_workers_router)
    app.state.settings = settings
    app.state.database = database
    app.state.planning_dataset = None
    app.state.planning_dataset_revision = None

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request_id = supplied if REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.monotonic()
        try:
            path = request.url.path
            method = request.method
            public_allowed = (
                (
                    method == "GET"
                    and path
                    in {
                        "/health",
                        "/ready",
                        "/v1/runtime",
                        "/v1/auth/me",
                        "/v1/catalog",
                        "/v1/tickets",
                        "/v1/worker/assignments",
                        "/v1/planning/location-dataset",
                    }
                )
                or (
                    method == "POST"
                    and path
                    in {
                        "/v1/auth/logout",
                        "/v1/auth/public-invite/exchange",
                        "/v1/auth/public-guest",
                        "/v1/tickets",
                    }
                )
                or (
                    method == "GET"
                    and re.fullmatch(
                        r"/v1/(?:tickets/[0-9a-fA-F-]+|worker/assignments/[0-9a-fA-F-]+|"
                        r"assignments/[0-9a-fA-F-]+/execution|"
                        r"tickets/[0-9a-fA-F-]+/catalog-location-candidates)",
                        path,
                    )
                )
                or (method == "PATCH" and re.fullmatch(r"/v1/tickets/[0-9a-fA-F-]+", path))
                or (
                    method == "POST"
                    and re.fullmatch(
                        r"/v1/tickets/[0-9a-fA-F-]+/(?:confirm|assign|confirm-assign|"
                        r"review-report|service-location|catalog-location)",
                        path,
                    )
                )
                or (
                    method == "POST"
                    and re.fullmatch(
                        r"/v1/worker/assignments/[0-9a-fA-F-]+/(?:start|depart|report|reports/[0-9a-fA-F-]+/confirm)",
                        path,
                    )
                )
                or (
                    method == "POST"
                    and re.fullmatch(
                        r"/v1/tickets/[0-9a-fA-F-]+/reports/[0-9a-fA-F-]+/request-correction",
                        path,
                    )
                )
                or (
                    method == "POST"
                    and re.fullmatch(r"/v1/assignments/[0-9a-fA-F-]+/end-visit", path)
                )
            )
            if settings.deployment_mode == "service" and not service_host_allowed(request):
                response = JSONResponse(
                    status_code=403,
                    content={"error": {"code": "host_rejected", "request_id": request_id}},
                )
            elif settings.deployment_mode == "public_demo" and not public_allowed:
                response = JSONResponse(
                    status_code=404,
                    content={
                        "error": {"code": "public_route_unavailable", "request_id": request_id}
                    },
                )
            else:
                response = await call_next(request)
        except Exception:
            # Never log exception text, request bodies, URLs, query strings or credentials.
            response = JSONResponse(
                status_code=500,
                content={"error": {"code": "internal_error", "request_id": request_id}},
            )
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = (
            "private, no-store" if request.url.path == "/v1/admin/export.csv" else "no-store"
        )
        logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
        )
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content={
                "error": {
                    "code": exc.detail
                    if isinstance(exc.detail, str) and re.fullmatch(r"[a-z_]+", exc.detail)
                    else f"http_{exc.status_code}",
                    "request_id": request.state.request_id,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "request_id": request.state.request_id,
                }
            },
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(environment=settings.app_env, llm_mode=runtime_mode(settings))

    @app.get("/ready", response_model=ReadyResponse, responses={503: {"model": NotReadyResponse}})
    async def ready():
        try:
            await database.ready()
        except Exception:
            return JSONResponse(status_code=503, content=NotReadyResponse().model_dump())
        return ReadyResponse()

    app.add_middleware(UploadBodyLimit)
    return app
