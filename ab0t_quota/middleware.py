"""QuotaGuard — FastAPI middleware for automatic rate limiting.

Drop-in replacement for all the per-service rate limiters. Handles
the RATE counter type automatically on every request.

Usage:
    app.add_middleware(
        QuotaGuard,
        engine=engine,
        resource_key="api.requests_per_hour",
        org_extractor=lambda request: request.state.user.org_id,
    )
"""

from __future__ import annotations

import logging
from typing import Optional, Callable, Awaitable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .engine import QuotaEngine
from .models.requests import QuotaCheckRequest, QuotaIncrementRequest

logger = logging.getLogger("ab0t_quota.middleware")

# Paths that skip quota checks
DEFAULT_EXEMPT_PATHS = frozenset({
    "/health", "/api/health", "/metrics", "/docs", "/redoc", "/openapi.json",
})


class QuotaGuard(BaseHTTPMiddleware):
    """FastAPI middleware that enforces rate limits via the QuotaEngine.

    For each non-exempt request:
    1. Extract org_id from the request (via org_extractor)
    2. Check the rate counter
    3. If denied → return 429 with standard body
    4. If allowed → increment counter and proceed
    """

    def __init__(
        self,
        app,
        engine: QuotaEngine,
        resource_key: str = "api.requests_per_hour",
        org_extractor: Optional[Callable[[Request], Awaitable[Optional[str]]]] = None,
        exempt_paths: Optional[frozenset[str]] = None,
        enabled: bool = True,
        fail_open: bool = False,
        fail_open_error_threshold: int = 0,
    ):
        super().__init__(app)
        self._engine = engine
        self._resource_key = resource_key
        self._org_extractor = org_extractor or self._default_org_extractor
        self._exempt_paths = exempt_paths or DEFAULT_EXEMPT_PATHS
        self._enabled = enabled
        self._fail_open = fail_open
        self._fail_open_error_threshold = fail_open_error_threshold
        self._consecutive_errors = 0

    async def dispatch(self, request: Request, call_next):
        if not self._enabled:
            return await call_next(request)

        if request.url.path in self._exempt_paths:
            return await call_next(request)

        org_id = await self._org_extractor(request)
        if not org_id:
            return await call_next(request)

        try:
            result = await self._engine.check(
                QuotaCheckRequest(
                    org_id=org_id,
                    resource_key=self._resource_key,
                    increment=1.0,
                )
            )
            self._consecutive_errors = 0
        except Exception as e:
            self._consecutive_errors += 1
            logger.error(
                "quota_check_error error=%s org_id=%s consecutive_errors=%d fail_mode=%s",
                str(e), org_id, self._consecutive_errors,
                "open" if self._fail_open else "closed",
            )
            if self._fail_open and (
                self._fail_open_error_threshold == 0
                or self._consecutive_errors < self._fail_open_error_threshold
            ):
                return await call_next(request)
            return JSONResponse(
                status_code=503,
                content={"error": "quota_service_unavailable", "detail": "Quota enforcement is temporarily unavailable."},
            )

        if result.denied:
            return JSONResponse(
                status_code=429,
                content=result.to_api_error(),
                headers={
                    "Retry-After": str(result.retry_after or 60),
                    "X-Quota-Limit": str(result.limit or ""),
                    "X-Quota-Current": str(result.current),
                    "X-Quota-Resource": result.resource_key,
                },
            )

        # Allowed — record the request
        try:
            await self._engine.increment(
                QuotaIncrementRequest(
                    org_id=org_id,
                    resource_key=self._resource_key,
                    delta=1.0,
                )
            )
        except Exception as e:
            logger.error("quota_increment_error error=%s org_id=%s", str(e), org_id)

        response = await call_next(request)

        # Add quota headers to response
        if result.limit is not None:
            response.headers["X-Quota-Limit"] = str(int(result.limit))
            response.headers["X-Quota-Remaining"] = str(int(result.remaining or 0))

        return response

    @staticmethod
    async def _default_org_extractor(request: Request) -> Optional[str]:
        """Default: read org_id from request.state.user (set by ab0t-auth)."""
        user = getattr(request.state, "user", None)
        if user is None:
            return None
        return getattr(user, "org_id", None)
