"""Observe polling outages, quiet isolated conflicts and bound administrator notifications."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import os
import time

import httpx
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import ExtBot

from i18n import get_language
from logger import error, info, warning
from safe_diagnostics import exception_summary, telegram_conflict_reason, telegram_details

SUMMARY_RETRY_SEC = 60
SUMMARY_SEND_TIMEOUT_SEC = 10
POLLING_CONFLICT_REPEAT_WINDOW_SEC = 120
POLLING_CONFLICT_ALERT_INTERVAL_SEC = 1800
_OBSERVED = object()
_MARKER = '_polling_health_observed'
_CONFLICT_MARKER = '_polling_conflict_admission'


@dataclass(frozen=True)
class PollingSettings:
    enabled: bool = True
    grace: float = 120

    @classmethod
    def from_env(cls, environ=None):
        """Alert convenience settings must not prevent the rest of the bot starting."""
        environ = os.environ if environ is None else environ
        flag = environ.get('POLLING_NETWORK_ALERTS_ENABLED', 'true').strip().lower()
        if flag not in {'true', '1', 'yes', 'on', 'false', '0', 'no', 'off'}:
            error('Invalid POLLING_NETWORK_ALERTS_ENABLED; aggregation disabled')
            return cls(enabled=False)
        if flag in {'false', '0', 'no', 'off'}:
            return cls(enabled=False)
        try:
            grace = float(environ.get('POLLING_NETWORK_ALERT_GRACE_SEC', '120'))
            if not math.isfinite(grace) or grace <= 0:
                raise ValueError
        except (TypeError, ValueError):
            error('Invalid POLLING_NETWORK_ALERT_GRACE_SEC; using 120 seconds')
            grace = 120
        return cls(grace=grace)


def is_transport_failure(exception):
    """Classify typed, polling-only transport evidence, without inspecting messages."""
    if not isinstance(exception, NetworkError) or isinstance(exception, BadRequest):
        return False
    chain, seen = [], set()
    current = exception
    while isinstance(current, BaseException) and id(current) not in seen and len(chain) < 16:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    if any(isinstance(item, (httpx.PoolTimeout, httpx.LocalProtocolError)) for item in chain):
        return False
    return isinstance(exception, TimedOut) or any(
        isinstance(
            item,
            (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.WriteError,
                httpx.CloseError,
                httpx.RemoteProtocolError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
            ),
        )
        for item in chain
    )


def observed_polling_error(exception):
    """Only the observer's exact marker bypasses ordinary immediate admin alerts."""
    return getattr(exception, _MARKER, None) is _OBSERVED


@dataclass(frozen=True)
class _ConflictAdmission:
    monitor: 'PollingMonitor'
    generation: object

    def claim(self, admin_id):
        """Reserve one actual recipient attempt before the generic handler awaits I/O."""
        return self.monitor.claim_conflict_attempt(admin_id, self.generation)


def polling_conflict_admission(exception):
    """Only the polling observer can attach a typed, already-logged admission token."""
    admission = getattr(exception, _CONFLICT_MARKER, None)
    return admission if isinstance(admission, _ConflictAdmission) else None


def compact_diagnostic(exception):
    """Admin summaries need type/cause names; full safe frame evidence stays in logs."""
    cause = exception.__cause__ or (None if exception.__suppress_context__ else exception.__context__)
    name = type(exception).__name__
    reason = telegram_conflict_reason(exception)
    if reason:
        name += f'({reason})'
    return f'{name} ← {type(cause).__name__}' if cause is not None else name


class PollingAwareBot(ExtBot):
    """Preserve PTB results/retries; observer failures cannot replace polling outcomes."""

    __slots__ = ('_polling_observer',)

    def __init__(self, *args, polling_observer, **kwargs):
        super().__init__(*args, **kwargs)
        self._polling_observer = polling_observer

    async def get_updates(self, *args, **kwargs):
        try:
            result = await super().get_updates(*args, **kwargs)
        except Exception as exc:
            try:
                if self._polling_observer.observe_failure(exc):
                    setattr(exc, _MARKER, _OBSERVED)
            except Exception as observer_error:  # pylint: disable=broad-exception-caught
                if observed_polling_error(exc):
                    delattr(exc, _MARKER)
                if polling_conflict_admission(exc) is not None:
                    delattr(exc, _CONFLICT_MARKER)
                # Even broken diagnostic logging must preserve the original polling outcome.
                with suppress(Exception):
                    error(
                        'Polling observer failed %s original=%s',
                        exception_summary(observer_error),
                        exception_summary(exc),
                    )
            raise
        try:
            self._polling_observer.observe_success()
        except Exception as observer_error:  # pylint: disable=broad-exception-caught
            with suppress(Exception):
                error('Polling observer failed %s', exception_summary(observer_error))
        return result


@dataclass(frozen=True)
class IncidentSummary:
    incidents: int
    duration: float
    failures: int
    ended_at: datetime
    diagnostic: str

    def merge(self, other):
        """Keep counts/duration and the latest completion evidence, never a message queue."""
        latest = other if other.ended_at >= self.ended_at else self
        return IncidentSummary(
            self.incidents + other.incidents,
            self.duration + other.duration,
            self.failures + other.failures,
            latest.ended_at,
            latest.diagnostic,
        )


@dataclass
class ActiveIncident:
    started: float
    failures: int
    diagnostic: str


class PollingIncidents:
    """Pure policy: no grace timer, I/O, stored exceptions or administrator state."""

    def __init__(self, grace):
        self.grace = grace
        self.active = None

    def fail(self, now, diagnostic):
        if self.active is None:
            self.active = ActiveIncident(now, 0, diagnostic)
        self.active.failures += 1
        self.active.diagnostic = diagnostic

    def recover(self, now, ended_at):
        if self.active is None:
            return None
        incident, self.active = self.active, None
        return IncidentSummary(1, max(0, now - incident.started), incident.failures, ended_at, incident.diagnostic)


@dataclass
class Recipient:
    pending: IncidentSummary | None = None
    next_attempt: float = 0
    disabled: bool = False
    next_conflict_attempt: float | None = None

    def add(self, summary):
        self.pending = self.pending.merge(summary) if self.pending else summary

    def conflict_due(self, now):
        return self.next_conflict_attempt is None or now >= self.next_conflict_attempt


def summary_text(summary, language):
    """Describe completed observations rather than claim that current connectivity is healthy."""
    completion = summary.ended_at.strftime('%Y-%m-%d %H:%M:%S UTC')
    if language in {'uk', 'ua'}:
        return (
            'Підсумок збоїв отримання повідомлень Telegram.\n'
            f'Завершених збоїв: {summary.incidents}. Загальна спостережена тривалість: {summary.duration:.1f} с.\n'
            f'Невдалих спроб: {summary.failures}. Останній успішний запит після збою: {completion}.\n'
            f'Остання діагностика: {summary.diagnostic}'
        )
    return (
        'Telegram polling incident summary.\n'
        f'Completed incidents: {summary.incidents}. Total observed unavailable duration: {summary.duration:.1f} s.\n'
        f'Failed attempts: {summary.failures}. Latest successful poll after an incident: {completion}.\n'
        f'Latest diagnostic: {summary.diagnostic}'
    )


class PollingMonitor:
    """One active incident, one pending aggregate per admin, one short-lived sender."""

    def __init__(self, settings, admin_ids='', notifications=False, *, clock=time.monotonic, wall_clock=None):
        self.settings = settings
        self.notifications = notifications
        self.clock = clock
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.language = get_language()
        self.incidents = PollingIncidents(settings.grace)
        self.recipients = {value.strip(): Recipient() for value in (admin_ids or '').split(',') if value.strip()}
        self.application = None
        self.started = False
        self.sender_task = None
        self.last_conflict_at = None
        self.conflict_generation = object()

    def start(self, application):
        self.application = application
        self.started = True
        self.conflict_generation = object()
        self.last_conflict_at = None
        for recipient in self.recipients.values():
            recipient.next_conflict_attempt = None

    def is_active(self):
        return bool(
            self.settings.enabled
            and self.started
            and self.application
            and self.application.updater
            and self.application.updater.running
        )

    def observe_failure(self, exception):
        if not self.is_active():
            return False
        if telegram_conflict_reason(exception) == 'polling_conflict':
            return self._observe_conflict(exception)
        if not is_transport_failure(exception):
            return False
        diagnostic = exception_summary(exception)
        now = self.clock()
        log = warning if isinstance(exception, TimedOut) else error
        log('Polling failure operation=get_updates %s', diagnostic)
        if self.incidents.active is None:
            info('Polling incident started operation=get_updates')
        self.incidents.fail(now, compact_diagnostic(exception))
        return True

    def _observe_conflict(self, exception):
        now = self.clock()
        repeated = (
            self.last_conflict_at is not None and 0 <= now - self.last_conflict_at <= POLLING_CONFLICT_REPEAT_WINDOW_SEC
        )
        log = error if repeated else warning
        log('Polling conflict operation=get_updates repeated=%s %s', repeated, exception_summary(exception))
        self.last_conflict_at = now
        if (
            repeated
            and self.notifications
            and any(not recipient.disabled and recipient.conflict_due(now) for recipient in self.recipients.values())
        ):
            setattr(exception, _CONFLICT_MARKER, _ConflictAdmission(self, self.conflict_generation))
            return False
        return True

    def claim_conflict_attempt(self, admin_id, generation):
        """Enforce actual per-admin timing even for slow/overlapping callback waves."""
        if generation is not self.conflict_generation or not self.is_active() or not self.notifications:
            return False
        recipient = self.recipients.get(admin_id)
        now = self.clock()
        if recipient is None or recipient.disabled or not recipient.conflict_due(now):
            return False
        recipient.next_conflict_attempt = now + POLLING_CONFLICT_ALERT_INTERVAL_SEC
        return True

    def observe_success(self):
        if not self.is_active():
            return
        summary = self.incidents.recover(self.clock(), self.wall_clock())
        if summary:
            reportable = summary.duration >= self.settings.grace
            info(
                'Polling incident completed duration_sec=%.1f failures=%d below_grace=%s',
                summary.duration,
                summary.failures,
                not reportable,
            )
            if reportable and self.notifications:
                for recipient in self.recipients.values():
                    if not recipient.disabled:
                        recipient.add(summary)
        self._kick_sender()

    def _kick_sender(self):
        if not self.notifications or self.sender_task is not None:
            return
        if any(
            recipient.pending and not recipient.disabled and self.clock() >= recipient.next_attempt
            for recipient in self.recipients.values()
        ):
            self.sender_task = asyncio.create_task(self._dispatch(), name='polling-incident-summary')
            self.sender_task.add_done_callback(self._sender_done)

    def _sender_done(self, task):
        if self.sender_task is task:
            self.sender_task = None
        if not task.cancelled():
            exception = task.exception()
            if exception is not None:
                # Even a failure outside the per-send guard must not create a task on every poll.
                retry_at = self.clock() + SUMMARY_RETRY_SEC
                for recipient in self.recipients.values():
                    if recipient.pending is not None:
                        recipient.next_attempt = max(recipient.next_attempt, retry_at)
                error('Polling summary task failed %s', exception_summary(exception))

    async def _dispatch(self):
        for admin_id, recipient in self.recipients.items():
            if not self.is_active() or self.incidents.active is not None:
                return
            if recipient.disabled or recipient.pending is None or self.clock() < recipient.next_attempt:
                continue
            # Detach the immutable snapshot before awaiting; new recoveries go into pending.
            recipient.next_attempt = self.clock() + SUMMARY_RETRY_SEC
            snapshot, recipient.pending = recipient.pending, None
            try:
                async with asyncio.timeout(SUMMARY_SEND_TIMEOUT_SEC):
                    await self.application.bot.send_message(
                        chat_id=admin_id,
                        text=summary_text(snapshot, self.language),
                        parse_mode=None,
                        disable_web_page_preview=True,
                        connect_timeout=5,
                        pool_timeout=5,
                        read_timeout=5,
                        write_timeout=5,
                    )
            except asyncio.CancelledError:
                recipient.add(snapshot)
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                recipient.add(snapshot)
                if isinstance(exc, Forbidden) or (
                    isinstance(exc, BadRequest) and 'telegram_error=chat_not_found' in telegram_details(exc)
                ):
                    recipient.disabled = True
                    recipient.pending = None
                if isinstance(exc, RetryAfter):
                    delay = exc.retry_after
                    delay = delay.total_seconds() if isinstance(delay, timedelta) else delay
                    if type(delay) in (int, float) and math.isfinite(delay) and delay > 0:
                        recipient.next_attempt = max(recipient.next_attempt, self.clock() + delay)
                error('Polling summary delivery failed %s', exception_summary(exc))

    async def close(self):
        """Close before the bot request transports; safe again after partial startup/shutdown."""
        self.started = False
        task = self.sender_task
        if task is not None:
            task.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await task
            except Exception as exc:  # pylint: disable=broad-exception-caught
                error('Polling summary shutdown failed %s', exception_summary(exc))
        self.sender_task = None
        self.recipients.clear()
        self.incidents.active = None
        self.application = None
        self.last_conflict_at = None
        self.conflict_generation = object()
