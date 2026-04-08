"""
API key authentication dependency for recruiter-facing routes.

Usage:
    from auth.dependencies import verify_api_key

    @router.get("/endpoint")
    async def my_endpoint(api_key: str = Depends(verify_api_key)):
        ...

Dev mode: if settings.api_keys is empty, all requests are allowed through.
Production: set API_KEYS=["key1","key2"] in .env to enforce auth.
"""

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from settings import settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    """
    Validates the X-API-Key header.
    - Empty settings.api_keys → dev mode, all requests pass through.
    - Non-empty settings.api_keys → key must be in the list or 403.
    """
    if not settings.api_keys:
        return api_key or ""   # dev mode: no auth
    if api_key and api_key in settings.api_keys:
        return api_key
    raise HTTPException(status_code=403, detail="Invalid or missing API key")
