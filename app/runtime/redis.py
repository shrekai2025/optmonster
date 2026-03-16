from __future__ import annotations

import math
import random
import uuid
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from app.runtime.settings import Settings


class RuntimeCoordinator:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings
        self.queue_key = "runtime:fetch_queue"
        self.pending_key = "runtime:fetch_pending"
        self.action_queue_key = "runtime:action_queue"
        self.action_pending_key = "runtime:action_pending"
        self.backoff_prefix = "runtime:backoff:"
        self.lock_prefix = "runtime:lock:"
        self.action_counter_prefix = "runtime:action_counter:"
        self.action_cooldown_prefix = "runtime:action_cooldown:"

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    async def enqueue_fetch(self, account_id: str) -> bool:
        if await self.redis.sadd(self.pending_key, account_id):
            await self.redis.rpush(self.queue_key, account_id)
            return True
        return False

    async def dequeue_fetch(self, block_timeout: int = 1) -> str | None:
        item = await self.redis.blpop(self.queue_key, timeout=block_timeout)
        if item is None:
            return None
        _, account_id = item
        await self.redis.srem(self.pending_key, account_id)
        return str(account_id)

    async def enqueue_action(self, action_id: int) -> bool:
        item = str(action_id)
        if await self.redis.sadd(self.action_pending_key, item):
            await self.redis.rpush(self.action_queue_key, item)
            return True
        return False

    async def dequeue_action(self, block_timeout: int = 1) -> int | None:
        item = await self.redis.blpop(self.action_queue_key, timeout=block_timeout)
        if item is None:
            return None
        _, action_id = item
        await self.redis.srem(self.action_pending_key, action_id)
        return int(action_id)

    async def acquire_account_lock(self, account_id: str) -> str | None:
        key = f"{self.lock_prefix}{account_id}"
        token = str(uuid.uuid4())
        acquired = await self.redis.set(
            key,
            token,
            ex=self.settings.lock_ttl_seconds,
            nx=True,
        )
        return token if acquired else None

    async def release_account_lock(self, account_id: str, token: str) -> None:
        key = f"{self.lock_prefix}{account_id}"
        current = await self.redis.get(key)
        if current == token:
            await self.redis.delete(key)

    async def backoff_ttl(self, account_id: str) -> int:
        ttl = await self.redis.ttl(f"{self.backoff_prefix}{account_id}")
        return max(int(ttl), 0)

    async def clear_backoff(self, account_id: str) -> None:
        await self.redis.delete(f"{self.backoff_prefix}{account_id}")

    async def schedule_backoff(self, account_id: str, failure_streak: int) -> int:
        exponent = max(failure_streak - 1, 0)
        ttl = min(
            self.settings.backoff_max_seconds,
            int(self.settings.backoff_base_seconds * math.pow(2, exponent)),
        )
        await self.redis.setex(f"{self.backoff_prefix}{account_id}", ttl, "1")
        return ttl

    async def get_daily_action_count(
        self,
        account_id: str,
        action_type: str,
        at: datetime,
    ) -> int:
        value = await self.redis.get(self._daily_action_key(account_id, action_type, at))
        return int(value or 0)

    async def get_action_cooldown_until(self, account_id: str) -> datetime | None:
        value = await self.redis.get(f"{self.action_cooldown_prefix}{account_id}")
        if value is None:
            return None
        return datetime.fromtimestamp(float(value), tz=UTC)

    async def record_action(
        self,
        account_id: str,
        action_type: str,
        at: datetime,
        *,
        min_interval_minutes: int,
    ) -> datetime:
        key = self._daily_action_key(account_id, action_type, at)
        ttl = max(self.seconds_until_daily_reset(at) + 86400, 1)
        pipeline = self.redis.pipeline()
        pipeline.incr(key)
        pipeline.expire(key, ttl)

        jitter = random.randint(0, max(self.settings.action_interval_jitter_seconds, 0))
        cooldown_until = at + timedelta(minutes=min_interval_minutes, seconds=jitter)
        pipeline.set(
            f"{self.action_cooldown_prefix}{account_id}",
            str(cooldown_until.timestamp()),
            ex=max(int((cooldown_until - at).total_seconds()) + 3600, 3600),
        )
        await pipeline.execute()
        return cooldown_until

    def seconds_until_daily_reset(self, at: datetime) -> int:
        localized = at.astimezone(self.settings.timezone)
        next_day = (localized + timedelta(days=1)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return max(int((next_day - localized).total_seconds()), 0)

    def _daily_action_key(self, account_id: str, action_type: str, at: datetime) -> str:
        day = at.astimezone(self.settings.timezone).date().isoformat()
        return f"{self.action_counter_prefix}{account_id}:{action_type}:{day}"
