from __future__ import annotations

import hashlib
import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from starlette.status import HTTP_401_UNAUTHORIZED

from app.config import Settings, get_settings


async def verify_internal_hmac(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> bytes:
    body = await request.body()
    received = request.headers.get("X-Internal-Auth")
    if not received:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED, detail={"error": "missing_auth"}
        )
    expected = hmac.new(
        settings.internal_api_auth_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(received, expected):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED, detail={"error": "bad_auth"}
        )
    return body
