from fastapi import HTTPException, Request
from redis import Redis

from app.core import settings


def enforce(request: Request, bucket: str, limit: int) -> None:
    """Fixed-window Redis limiter; auth and writes fail closed if Redis is unavailable."""
    client_ip = request.client.host if request.client else "unknown"
    key = f"docmind:rate:{bucket}:{client_ip}"
    try:
        redis = Redis.from_url(settings().effective_redis_url, decode_responses=True, socket_connect_timeout=1)
        current = redis.incr(key)
        if current == 1:
            redis.expire(key, 60)
    except Exception:
        raise HTTPException(503, "RATE_LIMIT_UNAVAILABLE")
    if current > limit:
        raise HTTPException(429, "RATE_LIMITED", headers={"Retry-After": "60"})
