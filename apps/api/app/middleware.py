"""Bound multipart transport before parsing or temporary-file creation."""

import json
import logging
import re
import uuid

from starlette.responses import JSONResponse

from app.storage import MAX_FILE

MAX_MULTIPART_BODY = MAX_FILE + 64 * 1024
MAX_CATALOG_MULTIPART_BODY = 32_000_000 + 64 * 1024


class UploadBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] not in {"/v1/uploads", "/v1/admin/catalog-imports"}
        ):
            return await self.app(scope, receive, send)
        max_body = (
            MAX_CATALOG_MULTIPART_BODY
            if scope["path"] == "/v1/admin/catalog-imports"
            else MAX_MULTIPART_BODY
        )
        messages = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            total += len(message.get("body", b""))
            if total > max_body:
                headers = dict(scope.get("headers", []))
                candidate = headers.get(b"x-request-id", b"").decode("latin1")
                request_id = (
                    candidate
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", candidate)
                    else uuid.uuid4().hex
                )
                logging.getLogger("baroai.http").info(
                    json.dumps(
                        {
                            "event": "http_request",
                            "request_id": request_id,
                            "method": "POST",
                            "status": 413,
                        }
                    )
                )
                response = JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "upload_too_large",
                            "request_id": request_id,
                        }
                    },
                    headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
                )
                return await response(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break
        queued = iter(messages)

        async def replay_receive():
            message = next(queued, None)
            return message if message is not None else await receive()

        await self.app(scope, replay_receive, send)
