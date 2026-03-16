from __future__ import annotations

import asyncio
import logging
from time import monotonic

from app.runtime.container import build_container, shutdown_container
from app.runtime.settings import get_settings

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    settings = get_settings()
    container = await build_container(settings)
    last_sweep = 0.0

    try:
        while True:
            now = monotonic()
            if now - last_sweep >= settings.worker_poll_interval_seconds:
                await container.account_service.reload_configs()
                due_accounts = await container.fetch_service.list_due_accounts()
                for account_id in due_accounts:
                    await container.runtime_coordinator.enqueue_fetch(account_id)
                last_sweep = now

            action_id = await container.runtime_coordinator.dequeue_action(block_timeout=1)
            if action_id is not None:
                await container.action_service.process_action(action_id)
                continue

            account_id = await container.runtime_coordinator.dequeue_fetch(block_timeout=1)
            if account_id is None:
                await asyncio.sleep(0.2)
                continue

            await container.fetch_service.fetch_account(account_id, trigger="scheduled")
    finally:
        await shutdown_container(container)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
