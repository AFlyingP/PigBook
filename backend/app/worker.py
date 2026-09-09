"""Background worker process and supervision.

Fixed public interface (Spec 3.4):
    main() -> None

Supervises independent asyncio tasks:
- hold expiry scheduler (every 30 seconds)
- heartbeat monitoring (every 10 seconds)
- hourly maintenance (every 3600 seconds)
- the notification dispatcher

Each operation owns its own AsyncSession; sessions are never shared across tasks.
Tasks do not hold database sessions across long awaits.
Uncaught task failures cancel sibling tasks and stop the process with non-zero exit.
Graceful shutdown stops new work and waits at most 25 seconds for in-flight operations.
"""

import asyncio
import logging
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.notifications.adapters import ConsoleEmailAdapter, EmailAdapter
from app.notifications.handler import dispatch_event
from app.notifications.maintenance import run_maintenance
from app.notifications.outbox import claim_batch, recover_stale_leases, renew_lease
from app.waitlist.scheduler import HoldExpiryScheduler, record_heartbeat

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("worker")

SHUTDOWN_BUDGET_SECONDS: float = 25.0
HEARTBEAT_INTERVAL_SECONDS: float = 10.0
SCHEDULER_INTERVAL_SECONDS: float = 30.0
MAINTENANCE_INTERVAL_SECONDS: float = 3600.0
DISPATCHER_POLL_INTERVAL_SECONDS: float = 1.0

TaskCallable = Callable[["WorkerSupervisor"], Awaitable[None]]


class WorkerSupervisor:
    """Supervises independent background worker tasks with lifecycle controls."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        *,
        shutdown_budget: float = SHUTDOWN_BUDGET_SECONDS,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
        scheduler_interval: float = SCHEDULER_INTERVAL_SECONDS,
        maintenance_interval: float = MAINTENANCE_INTERVAL_SECONDS,
        registered_tasks: list[TaskCallable] | None = None,
        resource_ids: Sequence[uuid.UUID] | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker or get_sessionmaker()
        self.shutdown_budget = shutdown_budget
        self.heartbeat_interval = heartbeat_interval
        self.scheduler_interval = scheduler_interval
        self.maintenance_interval = maintenance_interval
        self.resource_ids = resource_ids

        self._custom_tasks: list[TaskCallable] = list(registered_tasks or [])
        self._stopping = asyncio.Event()
        self._fatal_error: BaseException | None = None
        self._running_tasks: set[asyncio.Task[None]] = set()

    @property
    def is_stopping(self) -> bool:
        return self._stopping.is_set()

    def register_task(self, task_func: TaskCallable) -> None:
        """Register an additional task (e.g. notification dispatcher)."""
        self._custom_tasks.append(task_func)

    def request_shutdown(self) -> None:
        """Signal all tasks to stop accepting new work."""
        if not self._stopping.is_set():
            logger.info("Shutdown requested on worker supervisor")
            self._stopping.set()

    async def _wait_or_stop(self, timeout: float) -> bool:
        """Wait for timeout seconds or until shutdown is requested.

        Returns True if shutdown was requested, False if timeout expired.
        """
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def _run_heartbeat_loop(self) -> None:
        """Update worker_heartbeat seen_at every 10 seconds."""

        async def _beat() -> None:
            async with self.sessionmaker() as session:
                async with session.begin():
                    await record_heartbeat(session, seen=True, expiry_scan=False)

        await _beat()
        while not self.is_stopping:
            if await self._wait_or_stop(self.heartbeat_interval):
                break
            await _beat()

    async def _run_scheduler_loop(self) -> None:
        """Run hold expiry and promotion cycle every 30 seconds."""
        scheduler = HoldExpiryScheduler(self.sessionmaker, resource_ids=self.resource_ids)
        if not self.is_stopping:
            await scheduler.run_cycle()
        while not self.is_stopping:
            if await self._wait_or_stop(self.scheduler_interval):
                break
            if not self.is_stopping:
                await scheduler.run_cycle()

    async def _run_maintenance_loop(self) -> None:
        """Run hourly data maintenance (idempotency and rate-limit cleanup)."""
        while not self.is_stopping:
            if await self._wait_or_stop(self.maintenance_interval):
                break
            if not self.is_stopping:
                await run_maintenance(self.sessionmaker)

    async def _wrap_task(self, task_func: TaskCallable, name: str) -> None:
        try:
            await task_func(self)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            logger.error(
                "Supervised task '%s' failed with uncaught exception: %s", name, exc, exc_info=True
            )
            self._fatal_error = exc
            self.request_shutdown()
            # Cancel sibling tasks immediately
            current = asyncio.current_task()
            for t in self._running_tasks:
                if t is not current and not t.done():
                    t.cancel()
            raise

    async def run(self) -> None:
        """Run all supervised tasks concurrently and await completion or fatal error."""
        all_callables: list[tuple[str, TaskCallable]] = [
            ("heartbeat", lambda s: s._run_heartbeat_loop()),
            ("scheduler", lambda s: s._run_scheduler_loop()),
            ("maintenance", lambda s: s._run_maintenance_loop()),
        ]
        for idx, custom in enumerate(self._custom_tasks):
            all_callables.append((f"custom_{idx}_{getattr(custom, '__name__', 'task')}", custom))

        for name, fn in all_callables:
            t = asyncio.create_task(self._wrap_task(fn, name), name=name)
            self._running_tasks.add(t)

        try:
            # Wait for all tasks or until any task raises an exception
            await asyncio.gather(*self._running_tasks)
        except BaseException:
            if self._fatal_error is not None:
                raise self._fatal_error
            raise
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        """Gracefully wait up to shutdown_budget for in-flight tasks, then cancel."""
        self.request_shutdown()
        pending = [t for t in self._running_tasks if not t.done()]
        if pending:
            logger.info(
                "Waiting up to %.1fs for %d running task(s) to finish...",
                self.shutdown_budget,
                len(pending),
            )
            done, remaining = await asyncio.wait(pending, timeout=self.shutdown_budget)
            if remaining:
                logger.warning(
                    "Shutdown budget expired (%.1fs); cancelling %d remaining task(s)",
                    self.shutdown_budget,
                    len(remaining),
                )
                for t in remaining:
                    t.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)


class OutboxDispatcher:
    """Outbox notification dispatcher polling and claiming due events (Spec 6.1, 13.4)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        adapter: EmailAdapter | None = None,
        settings: Settings | None = None,
        *,
        poll_interval: float = DISPATCHER_POLL_INTERVAL_SECONDS,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.settings = settings or get_settings()
        self.adapter = adapter
        self.poll_interval = poll_interval

    async def run(self, supervisor: WorkerSupervisor) -> None:
        """Run the polling and dispatch loop supervised independently."""
        email_enabled = getattr(self.settings, "EMAIL_ENABLED", False)
        if not email_enabled:
            logger.info(
                "OutboxDispatcher: EMAIL_ENABLED=false; notification delivery disabled; "
                "outbox rows stay pending"
            )
            while not supervisor.is_stopping:
                if await supervisor._wait_or_stop(self.poll_interval):
                    break
            return

        adapter = self.adapter
        if adapter is None:
            email_adapter_type = getattr(self.settings, "EMAIL_ADAPTER", "console")
            if email_adapter_type == "smtp":
                from app.notifications.adapters import SMTPEmailAdapter

                adapter = SMTPEmailAdapter(self.settings)
            else:
                adapter = ConsoleEmailAdapter(self.settings)

        logger.info("OutboxDispatcher: started with %s", type(adapter).__name__)
        while not supervisor.is_stopping:
            try:
                now = datetime.now(timezone.utc)
                # 1. Recover stale leases
                async with self.sessionmaker() as session:
                    async with session.begin():
                        await recover_stale_leases(session, now=now)

                # 2. Claim at most one due row (claim_batch commits before dispatch)
                async with self.sessionmaker() as session:
                    leases = await claim_batch(session, now=now)

                if not leases:
                    if await supervisor._wait_or_stop(self.poll_interval):
                        break
                    continue

                lease = leases[0]

                # 3. Renew owned lease immediately before dispatch
                renew_now = datetime.now(timezone.utc)
                async with self.sessionmaker() as session:
                    async with session.begin():
                        renewed = await renew_lease(session, lease=lease, now=renew_now)

                if not renewed:
                    logger.warning(
                        "OutboxDispatcher: lost ownership of lease %s; skipping dispatch",
                        lease.id,
                    )
                    continue

                # 4. Dispatch outside transaction
                await dispatch_event(
                    lease,
                    adapter,
                    sessionmaker=self.sessionmaker,
                    now=renew_now,
                    settings=self.settings,
                )

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("OutboxDispatcher cycle error: %s", exc, exc_info=True)
                if await supervisor._wait_or_stop(self.poll_interval):
                    break


def main() -> None:
    """Entry point for the background worker daemon (Spec 3.4)."""
    supervisor = WorkerSupervisor()
    settings = get_settings()
    dispatcher = OutboxDispatcher(supervisor.sessionmaker, settings=settings)
    supervisor.register_task(dispatcher.run)

    def _handle_signal(signum: int, frame: Any) -> None:
        logger.info("Signal %s received; initiating worker shutdown...", signum)
        supervisor.request_shutdown()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Starting CommonsBook background worker...")
    try:
        asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user")
    except BaseException as exc:
        logger.critical("Worker stopped due to unhandled fatal error: %s", exc)
        sys.exit(1)
    logger.info("Worker stopped cleanly")


if __name__ == "__main__":
    main()
