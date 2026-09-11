import hashlib

from fastapi import Request, HTTPException


async def require_api_key(request: Request) -> None:
    config = request.app.state.config
    if not config.api_key:
        return  # auth disabled when no key is configured
    key = request.headers.get("X-API-Key", "")
    if key != config.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def _session_identity(request: Request) -> str:
    supplied_key = request.headers.get("X-API-Key", "")
    remote = request.client.host if request.client else "unknown"
    identity = supplied_key or remote
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


async def bind_session_access(redis, config, request: Request, session_id: str) -> None:
    key = f"session-owner:{session_id}"
    await redis.set(key, _session_identity(request), ex=config.session_ttl, nx=True)
    await redis.expire(key, config.session_ttl)


async def assert_session_access(redis, request: Request, session_id: str) -> None:
    owner = await redis.get(f"session-owner:{session_id}")
    if owner and owner != _session_identity(request):
        raise HTTPException(status_code=403, detail="Session does not belong to this caller")
