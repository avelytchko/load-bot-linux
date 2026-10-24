"""Offline polling transport mapping, retrospective incidents, recipient isolation and lifecycle."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs

import httpx
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application
from telegram.request import HTTPXRequest

import general_error_handler
import polling_health
from polling_health import (
    PollingAwareBot,
    PollingMonitor,
    PollingSettings,
    is_transport_failure,
    observed_polling_error,
)


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def connection_error():
    exception = NetworkError('PRIVATE_SENTINEL')
    exception.__cause__ = httpx.ConnectError('PRIVATE_SENTINEL')
    return exception


class ConfigTests(unittest.TestCase):
    def test_defaults_and_explicit_boolean_spellings(self):
        self.assertEqual(PollingSettings.from_env({}), PollingSettings())
        for value in ('true', '1', 'yes', 'on', ' TRUE '):
            self.assertTrue(PollingSettings.from_env({'POLLING_NETWORK_ALERTS_ENABLED': value}).enabled)
        for value in ('false', '0', 'no', 'off', ' FALSE '):
            settings = PollingSettings.from_env(
                {'POLLING_NETWORK_ALERTS_ENABLED': value, 'POLLING_NETWORK_ALERT_GRACE_SEC': 'PRIVATE_SENTINEL'}
            )
            self.assertFalse(settings.enabled)

    def test_invalid_settings_fall_back_without_echoing_values(self):
        with self.assertLogs('logger', level='ERROR') as logs:
            self.assertFalse(PollingSettings.from_env({'POLLING_NETWORK_ALERTS_ENABLED': 'PRIVATE_SENTINEL'}).enabled)
            for value in ('PRIVATE_SENTINEL', 'nan', 'inf', '-inf', '0', '-1', ''):
                self.assertEqual(PollingSettings.from_env({'POLLING_NETWORK_ALERT_GRACE_SEC': value}).grace, 120)
        self.assertNotIn('PRIVATE_SENTINEL', '\n'.join(logs.output))
        self.assertEqual(PollingSettings.from_env({'POLLING_NETWORK_ALERT_GRACE_SEC': '2.5'}).grace, 2.5)

    def test_unknown_errors_and_cycles_are_not_broadly_suppressed(self):
        exception = NetworkError('Unknown')
        exception.__cause__ = exception
        self.assertFalse(is_transport_failure(exception))
        self.assertFalse(is_transport_failure(BadRequest('Bad request')))
        self.assertFalse(is_transport_failure(RetryAfter(2)))
        self.assertFalse(is_transport_failure(asyncio.CancelledError()))
        suppressed = NetworkError('Unknown')
        suppressed.__context__ = httpx.ConnectError('not a visible cause')
        suppressed.__suppress_context__ = True
        self.assertFalse(is_transport_failure(suppressed))


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.send = AsyncMock()
        self.app = SimpleNamespace(updater=SimpleNamespace(running=True), bot=SimpleNamespace(send_message=self.send))
        self.monitor = PollingMonitor(
            PollingSettings(),
            '1, 2,1, ,',
            True,
            clock=self.clock,
            wall_clock=lambda: datetime(2026, 10, 23, tzinfo=timezone.utc) + timedelta(seconds=self.clock()),
        )
        self.monitor.start(self.app)

    async def asyncTearDown(self):
        await self.monitor.close()

    def complete(self, duration=120, exception=None):
        self.monitor.observe_failure(exception or connection_error())
        self.clock.advance(duration)
        self.monitor.observe_success()

    async def drain(self):
        if self.monitor.sender_task:
            await self.monitor.sender_task
            await asyncio.sleep(0)

    async def test_short_incidents_and_quiet_polling_stay_silent_with_every_failure_logged(self):
        self.monitor.observe_success()
        with self.assertLogs('logger', level='ERROR') as logs:
            for _ in range(4):
                self.complete(10)
        self.assertEqual(len(logs.output), 4)
        self.assertNotIn('PRIVATE_SENTINEL', '\n'.join(logs.output))
        self.clock.advance(3600)
        self.monitor.observe_success()
        self.assertIsNone(self.monitor.sender_task)
        self.send.assert_not_awaited()

    async def test_exact_duration_boundary_and_no_alert_while_still_unavailable(self):
        self.complete(119.999)
        self.send.assert_not_awaited()
        self.complete(120)
        await self.drain()
        self.assertEqual(self.send.await_count, 2)
        self.complete(120.001)
        await self.drain()
        self.assertEqual(self.send.await_count, 4)
        self.monitor.observe_failure(connection_error())
        self.clock.advance(3600)
        self.monitor.observe_failure(connection_error())
        await self.drain()
        self.assertEqual(self.send.await_count, 4)
        self.monitor.observe_success()
        await self.drain()
        self.assertIn('3600.0', self.send.call_args.kwargs['text'])
        self.assertIn('2', self.send.call_args.kwargs['text'])

    async def test_partial_delivery_and_minimum_retry_interval(self):
        self.send.side_effect = [None, NetworkError('PRIVATE_SENTINEL'), None]
        self.complete()
        await self.drain()
        self.clock.advance(59.999)
        self.monitor.observe_success()
        await self.drain()
        self.assertEqual(self.send.await_count, 2)
        self.clock.advance(0.001)
        self.monitor.observe_success()
        await self.drain()
        self.assertEqual([call.kwargs['chat_id'] for call in self.send.call_args_list], ['1', '2', '2'])
        for call in self.send.call_args_list:
            self.assertIsNone(call.kwargs['parse_mode'])
            self.assertNotIn('PRIVATE_SENTINEL', call.kwargs['text'])
            self.assertIn('NetworkError ← ConnectError', call.kwargs['text'])
            self.assertNotIn('stack=', call.kwargs['text'])

    async def test_retry_after_extends_deadline_and_terminal_recipient_stops_retrying(self):
        self.send.side_effect = [RetryAfter(timedelta(seconds=300)), Forbidden('PRIVATE_SENTINEL'), None, None]
        self.complete()
        await self.drain()
        self.clock.advance(60)
        self.monitor.observe_success()
        await self.drain()
        self.assertEqual(self.send.await_count, 2)
        self.clock.advance(240)
        self.monitor.observe_success()
        await self.drain()
        self.assertEqual(self.send.await_count, 3)
        self.complete()
        await self.drain()
        self.assertEqual(self.send.call_args.kwargs['chat_id'], '1')
        self.assertTrue(self.monitor.recipients['2'].disabled)

    async def test_invalid_recipient_is_terminal_but_other_bad_requests_are_not(self):
        self.send.side_effect = [BadRequest('Chat not found'), BadRequest('PRIVATE_SENTINEL'), None]
        self.complete()
        await self.drain()
        self.assertTrue(self.monitor.recipients['1'].disabled)
        self.assertFalse(self.monitor.recipients['2'].disabled)
        self.clock.advance(60)
        self.monitor.observe_success()
        await self.drain()
        self.assertEqual(self.send.call_args.kwargs['chat_id'], '2')

    async def test_old_success_does_not_clear_new_pending_summary(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(**kwargs):
            if kwargs['chat_id'] == '1' and not entered.is_set():
                entered.set()
                await release.wait()

        self.send.side_effect = send
        self.complete()
        await entered.wait()
        self.complete()
        self.assertEqual(self.monitor.recipients['1'].pending.incidents, 1)
        release.set()
        await self.drain()
        self.assertEqual(self.monitor.recipients['1'].pending.incidents, 1)
        self.assertIsNone(self.monitor.recipients['2'].pending)
        self.monitor.observe_success()
        await self.drain()
        self.assertEqual([call.kwargs['chat_id'] for call in self.send.call_args_list], ['1', '2', '1'])
        self.assertIn('2', self.send.call_args_list[1].kwargs['text'])

    async def test_old_failure_merges_back_without_losing_new_completion(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(**kwargs):
            if kwargs['chat_id'] == '1':
                entered.set()
                await release.wait()
                raise NetworkError('PRIVATE_SENTINEL')

        self.send.side_effect = send
        self.complete()
        await entered.wait()
        self.complete()
        release.set()
        await self.drain()
        summary = self.monitor.recipients['1'].pending
        self.assertEqual((summary.incidents, summary.duration, summary.failures), (2, 240, 2))
        self.send.side_effect = None
        self.monitor.observe_success()
        await self.drain()
        self.assertIsNone(self.monitor.recipients['1'].pending)

    async def test_new_outage_defers_unsent_recovery_until_polling_really_recovers(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(**_kwargs):
            entered.set()
            await release.wait()

        self.send.side_effect = send
        self.complete()
        await entered.wait()
        self.monitor.observe_failure(connection_error())
        release.set()
        await self.drain()
        self.assertEqual(self.send.await_count, 1)
        self.assertIsNotNone(self.monitor.recipients['2'].pending)
        self.clock.advance(10)
        self.monitor.observe_success()
        await self.drain()
        self.assertEqual(self.send.await_count, 2)

    async def test_repeated_delivery_failures_coalesce_in_bounded_per_admin_state(self):
        self.send.side_effect = NetworkError('PRIVATE_SENTINEL')
        for _ in range(5):
            self.complete()
            await self.drain()
        self.assertEqual(len(self.monitor.recipients), 2)
        for recipient in self.monitor.recipients.values():
            self.assertEqual((recipient.pending.incidents, recipient.pending.duration), (5, 600))
        self.send.side_effect = None
        self.clock.advance(60)
        self.monitor.observe_success()
        await self.drain()
        self.assertTrue(all(recipient.pending is None for recipient in self.monitor.recipients.values()))
        self.assertIn('5', self.send.call_args.kwargs['text'])

    async def test_hung_send_is_bounded_and_does_not_prevent_other_admin(self):
        async def send(**kwargs):
            if kwargs['chat_id'] == '1':
                await asyncio.Future()

        self.send.side_effect = send
        with patch.object(polling_health, 'SUMMARY_SEND_TIMEOUT_SEC', 0.01):
            self.complete()
            await self.drain()
        self.assertEqual(self.send.await_count, 2)
        self.assertIsNotNone(self.monitor.recipients['1'].pending)
        self.assertIsNone(self.monitor.recipients['2'].pending)

    async def test_sender_internal_error_retains_pending_and_obeys_retry_deadline(self):
        with patch.object(polling_health, 'summary_text', side_effect=RuntimeError('PRIVATE_SENTINEL')):
            self.complete()
            with self.assertLogs('logger', level='ERROR') as logs:
                await self.drain()
        self.assertNotIn('PRIVATE_SENTINEL', '\n'.join(logs.output))
        self.monitor.observe_success()
        await self.drain()
        self.send.assert_not_awaited()
        self.clock.advance(60)
        self.monitor.observe_success()
        await self.drain()
        self.assertEqual(self.send.await_count, 2)

    async def test_unexpected_dispatch_failure_is_consumed_and_rate_limited(self):
        with patch.object(self.monitor, '_dispatch', new_callable=AsyncMock) as dispatch:
            dispatch.side_effect = RuntimeError('PRIVATE_SENTINEL')
            self.complete()
            task = self.monitor.sender_task
            with self.assertRaises(RuntimeError):
                await task
            await asyncio.sleep(0)
            self.monitor.observe_success()
            self.assertIsNone(self.monitor.sender_task)
            self.assertEqual(dispatch.await_count, 1)
        self.clock.advance(60)
        self.monitor.observe_success()
        await self.drain()
        self.assertEqual(self.send.await_count, 2)

    async def test_shutdown_cancels_and_awaits_sender_without_fabricating_recovery(self):
        entered = asyncio.Event()

        async def send(**_kwargs):
            entered.set()
            await asyncio.Future()

        self.send.side_effect = send
        self.complete()
        await entered.wait()
        task = self.monitor.sender_task
        await self.monitor.close()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.monitor.started)
        self.assertFalse(self.monitor.recipients)
        await self.monitor.close()
        self.monitor.observe_success()
        self.assertFalse(self.monitor.observe_failure(connection_error()))

    async def test_log_levels_language_alias_and_master_switch(self):
        with self.assertLogs('logger', level='WARNING') as logs:
            self.monitor.observe_failure(TimedOut())
            self.monitor.observe_failure(connection_error())
        self.assertIn('WARNING', logs.output[0])
        self.assertIn('ERROR', logs.output[1])
        for language, expected in [('uk', 'Підсумок'), ('ua', 'Підсумок'), ('en', 'Telegram')]:
            self.monitor.language = language
            self.clock.advance(120)
            self.monitor.observe_success()
            await self.drain()
            self.assertTrue(self.send.call_args.kwargs['text'].startswith(expected))
            self.monitor.observe_failure(connection_error())
        self.monitor.notifications = False
        self.clock.advance(120)
        count = self.send.await_count
        self.monitor.observe_success()
        await self.drain()
        self.assertEqual(count, self.send.await_count)

    async def test_disabled_absent_recipient_and_stopped_updater(self):
        for settings, admins, notifications in [
            (PollingSettings(False), '1', True),
            (PollingSettings(), '', True),
            (PollingSettings(), '1', False),
        ]:
            monitor = PollingMonitor(settings, admins, notifications, clock=self.clock)
            monitor.start(self.app)
            monitor.observe_failure(connection_error())
            self.clock.advance(120)
            monitor.observe_success()
            self.assertIsNone(monitor.sender_task)
            await monitor.close()
        self.app.updater.running = False
        self.assertFalse(self.monitor.observe_failure(connection_error()))
        self.monitor.observe_success()
        self.assertIsNone(self.monitor.incidents.active)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.send = AsyncMock()
        self.app = SimpleNamespace(updater=SimpleNamespace(running=True), bot=SimpleNamespace(send_message=self.send))
        self.monitor = PollingMonitor(PollingSettings(), '1', True, clock=self.clock)
        self.monitor.start(self.app)
        self.addAsyncCleanup(self.monitor.close)

    async def make_bot(self, outcomes, observer=None, success_event=None, request_log=None):
        outcomes = iter(outcomes)

        async def handle(request):
            if request.url.path.endswith('/getMe'):
                return httpx.Response(
                    200, json={'ok': True, 'result': {'id': 123, 'is_bot': True, 'first_name': 'Unit'}}
                )
            if not request.url.path.endswith('/getUpdates'):
                return httpx.Response(200, json={'ok': True, 'result': True})
            if request_log is not None:
                request_log.append(parse_qs(request.content.decode()))
            await asyncio.sleep(0)
            value = next(outcomes, 'ok')
            if isinstance(value, BaseException):
                raise value
            if isinstance(value, int):
                return httpx.Response(value, json={'ok': False, 'description': 'PRIVATE_SENTINEL'})
            if value == 'malformed':
                return httpx.Response(200, content=b'not JSON')
            if success_event:
                success_event.set()
            return httpx.Response(200, json={'ok': True, 'result': value if isinstance(value, list) else []})

        def request():
            return HTTPXRequest(httpx_kwargs={'transport': httpx.MockTransport(handle)})

        bot = PollingAwareBot(
            '123:dummy',
            request=request(),
            get_updates_request=request(),
            polling_observer=self.monitor if observer is None else observer,
        )
        await bot.initialize()
        self.addAsyncCleanup(bot.shutdown)
        return bot

    async def test_actual_transport_mapping_marks_same_exception_and_logs_once(self):
        for failure in (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.CloseError,
            httpx.RemoteProtocolError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
        ):
            bot = await self.make_bot([failure('PRIVATE_SENTINEL')])
            with self.assertLogs('logger', level='WARNING') as logs:
                with self.assertRaises(NetworkError) as caught:
                    await bot.get_updates(offset=12, timeout=0)
                exception = caught.exception
                self.assertIsInstance(exception.__cause__, failure)
                self.assertTrue(observed_polling_error(exception))
                self.assertEqual(
                    self.monitor.incidents.active.diagnostic, f'{type(exception).__name__} ← {failure.__name__}'
                )
                context = SimpleNamespace(error=exception, bot=SimpleNamespace(send_message=AsyncMock()))
                await general_error_handler.error_handler(None, context)
                context.bot.send_message.assert_not_awaited()
            self.assertEqual(len(logs.output), 1)
            self.assertIn('stack=', logs.output[0])
            self.assertNotIn('PRIVATE_SENTINEL', '\n'.join(logs.output))

    async def test_status_pool_local_protocol_unknown_and_malformed_errors_keep_ordinary_path(self):
        outcomes = [
            400,
            409,
            502,
            500,
            httpx.PoolTimeout('PRIVATE_SENTINEL'),
            httpx.LocalProtocolError('PRIVATE_SENTINEL'),
            RuntimeError('PRIVATE_SENTINEL'),
            'malformed',
        ]
        bot = await self.make_bot(outcomes)
        for _ in outcomes:
            with self.assertRaises(Exception) as caught:
                await bot.get_updates(timeout=0)
            self.assertFalse(observed_polling_error(caught.exception))
            self.assertFalse(is_transport_failure(caught.exception))
            context = SimpleNamespace(error=caught.exception, bot=SimpleNamespace(send_message=AsyncMock()))
            with patch.object(general_error_handler, 'send_error_to_admin', True), patch.object(
                general_error_handler, 'admins_chat_ids', '1'
            ), self.assertLogs('logger', level='ERROR') as logs:
                await general_error_handler.error_handler(None, context)
            context.bot.send_message.assert_awaited_once()
            self.assertNotIn('PRIVATE_SENTINEL', '\n'.join(logs.output))
            self.assertNotIn('PRIVATE_SENTINEL', context.bot.send_message.call_args.kwargs['text'])

    async def test_observer_failure_preserves_original_exception_and_success(self):
        observer = SimpleNamespace(
            observe_failure=Mock(side_effect=RuntimeError('PRIVATE_SENTINEL')),
            observe_success=Mock(side_effect=RuntimeError('PRIVATE_SENTINEL')),
        )
        bot = await self.make_bot([httpx.ConnectError('PRIVATE_SENTINEL'), 'ok'], observer)
        with self.assertLogs('logger', level='ERROR') as logs:
            with self.assertRaises(NetworkError) as caught:
                await bot.get_updates()
            self.assertIs(caught.exception, observer.observe_failure.call_args.args[0])
            self.assertFalse(observed_polling_error(caught.exception))
            self.assertEqual(await bot.get_updates(), ())
        self.assertNotIn('PRIVATE_SENTINEL', '\n'.join(logs.output))

    async def test_failure_during_recording_leaves_error_unmarked(self):
        bot = await self.make_bot([httpx.ConnectError('PRIVATE_SENTINEL')])
        with patch.object(self.monitor.incidents, 'fail', side_effect=RuntimeError('PRIVATE_SENTINEL')):
            with self.assertRaises(NetworkError) as caught:
                await bot.get_updates()
        self.assertFalse(observed_polling_error(caught.exception))

    async def test_cancelled_poll_does_not_create_incident(self):
        bot = await self.make_bot([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await bot.get_updates()
        self.assertIsNone(self.monitor.incidents.active)

    async def test_empty_poll_recovers_and_non_polling_success_does_not(self):
        bot = await self.make_bot([httpx.ConnectError('PRIVATE_SENTINEL'), 'ok'])
        with self.assertRaises(NetworkError):
            await bot.get_updates()
        self.clock.advance(120)
        await bot.get_me()
        self.assertIsNotNone(self.monitor.incidents.active)
        self.send.assert_not_awaited()
        self.assertEqual(await bot.get_updates(), ())
        await self.monitor.sender_task
        self.send.assert_awaited_once()

    async def test_poll_arguments_and_nonempty_results_are_preserved(self):
        requests = []
        bot = await self.make_bot([[{'update_id': 77}]], request_log=requests)
        result = await bot.get_updates(offset=12, timeout=4, allowed_updates=['message'])
        self.assertEqual([update.update_id for update in result], [77])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['offset'], ['12'])
        self.assertEqual(requests[0]['timeout'], ['4'])
        self.assertEqual(requests[0]['allowed_updates'], ['["message"]'])

    async def test_two_bot_instances_keep_separate_observers(self):
        observer = SimpleNamespace(observe_success=Mock(), observe_failure=Mock(return_value=False))
        first = await self.make_bot([httpx.ConnectError('PRIVATE_SENTINEL')])
        second = await self.make_bot(['ok'], observer)
        with self.assertRaises(NetworkError):
            await first.get_updates()
        self.assertEqual(await second.get_updates(), ())
        observer.observe_success.assert_called_once()
        observer.observe_failure.assert_not_called()
        self.assertIsNotNone(self.monitor.incidents.active)

    async def test_actual_updater_forwards_connect_error_but_not_timeout_and_ignores_stop_poll(self):
        successful = asyncio.Event()
        bot = await self.make_bot(
            [httpx.ConnectError('PRIVATE_SENTINEL'), httpx.ReadTimeout('PRIVATE_SENTINEL')], success_event=successful
        )
        self.monitor.notifications = False
        app = Application.builder().bot(bot).build()
        self.monitor.start(app)
        forwarded = []

        async def on_error(_update, context):
            forwarded.append(context.error)
            await general_error_handler.error_handler(None, context)

        app.add_error_handler(on_error)
        await app.initialize()
        await app.start()

        def callback(exception):
            # Same callback shape as Application.run_polling; no production Bot API.
            app.create_task(app.process_error(update=None, error=exception))

        original_failure = self.monitor.observe_failure
        original_recover = self.monitor.incidents.recover
        recovered_while_running = []

        def recover(*args):
            recovered_while_running.append(app.updater.running)
            return original_recover(*args)

        def observe(exception):
            result = original_failure(exception)
            if isinstance(exception, TimedOut):
                self.clock.advance(120)
            return result

        try:
            with patch.object(self.monitor, 'observe_failure', side_effect=observe), patch.object(
                self.monitor.incidents, 'recover', side_effect=recover
            ):
                await app.updater.start_polling(error_callback=callback)
                await asyncio.wait_for(successful.wait(), 5)
                await asyncio.sleep(0)
                await app.updater.stop()
            self.assertEqual(len(forwarded), 1)
            self.assertTrue(observed_polling_error(forwarded[0]))
            self.assertIsNone(self.monitor.incidents.active)
            self.assertTrue(recovered_while_running)
            self.assertTrue(all(recovered_while_running))
        finally:
            if app.updater.running:
                await app.updater.stop()
            await app.stop()
            await self.monitor.close()
            await app.shutdown()
