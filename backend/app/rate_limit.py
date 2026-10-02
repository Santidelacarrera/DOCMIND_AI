import hashlib

from fastapi import HTTPException, Request
from redis import Redis

from app.core import settings

WINDOW_SECONDS = 60


def enforce(request: Request, bucket: str, limit: int, subject: str | None = None) -> None:
    """Fixed-window Redis limiter; fails closed if Redis is unavailable.

    The window is keyed by client address, or by a hash of ``subject`` when given (for
    example a login email) so personal data is never stored in Redis keys.
    """
    if subject is not None:
        identity = hashlib.sha256(subject.encode()).hexdigest()[:32]
    else:
        identity = request.client.host if request.client else "unknown"
    key = f"docmind:rate:{bucket}:{identity}"
    try:
        redis = Redis.from_url(
            settings().effective_redis_url, decode_responses=True, socket_connect_timeout=1
        )
        # INCR and EXPIRE in one transaction so a crash can never leave a key without a TTL.
        pipe = redis.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, WINDOW_SECONDS, nx=True)
        current = int(pipe.execute()[0])
    except Exception:
        raise HTTPException(503, "RATE_LIMIT_UNAVAILABLE") from None
    if current > limit:
        raise HTTPException(429, "RATE_LIMITED", headers={"Retry-After": str(WINDOW_SECONDS)})
