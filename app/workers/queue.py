"""arq `WorkerSettings` + the API's enqueue pool (workers.md).

`arq app.workers.queue.WorkerSettings` (CLAUDE.md) runs the stage workers over Redis. Enqueue is
idempotent via the arq `_job_id` (set to the run id); status transitions + retries-with-backoff are
arq's, the dead-letter is its terminal-failure list. Only `attack` runs before M3.
"""

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import get_redis_settings
from app.workers.attack import attack_run


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_redis_settings().redis_url)


async def create_arq_pool() -> ArqRedis:
    """The enqueue pool the API holds (created at app startup, closed at shutdown)."""
    return await create_pool(_redis_settings())


class WorkerSettings:
    """arq worker entrypoint — each function is a thin wrapper that calls its service."""

    functions = [attack_run]
    redis_settings = _redis_settings()
    max_tries = 3  # retries-with-backoff, then dead-letter (run-lifecycle.md)
