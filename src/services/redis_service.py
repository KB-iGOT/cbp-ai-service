import json
import zlib
import pickle
from typing import Any, Optional

import redis.asyncio as aioredis

from ..core.configs import settings
from ..core.logger import logger

_redis_client: aioredis.Redis | None = None


def get_redis_client() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.REDIS_URL, decode_responses=False)
    return _redis_client


class RedisService:
    """Async Redis service with compressed pickle storage and JSON helpers."""

    # ------------------------------------------------------------------ #
    # Compressed pickle (for arbitrary Python objects)                    #
    # ------------------------------------------------------------------ #

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Serialize, compress and store any Python object. Permanent if ttl is None."""
        client = get_redis_client()
        data = zlib.compress(pickle.dumps(value))
        if ttl:
            await client.setex(key, ttl, data)
        else:
            await client.set(key, data)
        logger.debug(f"Redis SET {key} (ttl={ttl or 'permanent'})")

    async def get(self, key: str) -> Any | None:
        """Retrieve and decompress a pickled object. Returns None if missing."""
        client = get_redis_client()
        data = await client.get(key)
        if data is None:
            return None
        return pickle.loads(zlib.decompress(data))

    async def delete(self, key: str) -> None:
        client = get_redis_client()
        await client.delete(key)

    # ------------------------------------------------------------------ #
    # JSON helpers (for plain dicts / lists — human-readable in Redis)    #
    # ------------------------------------------------------------------ #

    async def set_json(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Store a JSON-serialisable value. Permanent if ttl is None."""
        client = get_redis_client()
        data = json.dumps(value).encode()
        if ttl:
            await client.setex(key, ttl, data)
        else:
            await client.set(key, data)
        logger.debug(f"Redis SET_JSON {key} (ttl={ttl or 'permanent'})")

    async def get_json(self, key: str) -> Any | None:
        """Retrieve a JSON value. Returns None if missing."""
        client = get_redis_client()
        data = await client.get(key)
        if data is None:
            return None
        return json.loads(data)

    # ------------------------------------------------------------------ #
    # Pipeline helpers (batch get / set in one round-trip)                #
    # ------------------------------------------------------------------ #

    async def mget_json(self, keys: list[str]) -> list[Any | None]:
        """Fetch multiple JSON keys in one pipeline. Returns list aligned to keys."""
        client = get_redis_client()
        pipe = client.pipeline()
        for k in keys:
            pipe.get(k)
        raw_values = await pipe.execute()
        return [json.loads(v) if v is not None else None for v in raw_values]

    async def mset_json(self, mapping: dict[str, Any], ttl: Optional[int] = None) -> None:
        """Store multiple JSON values in one pipeline. Permanent if ttl is None."""
        client = get_redis_client()
        pipe = client.pipeline()
        for k, v in mapping.items():
            data = json.dumps(v).encode()
            if ttl:
                pipe.setex(k, ttl, data)
            else:
                pipe.set(k, data)
        await pipe.execute()
        logger.debug(f"Redis MSET_JSON {len(mapping)} keys (ttl={ttl or 'permanent'})")

    async def ping(self) -> bool:
        try:
            return await get_redis_client().ping()
        except Exception:
            return False


redis_service = RedisService()
