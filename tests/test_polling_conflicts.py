"""Real PTB transport regressions for isolated/repeated polling conflicts, fully offline."""

import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
from telegram.error import Conflict, Forbidden, TelegramError
from telegram.ext import Application
from telegram.request import HTTPXRequest

import general_error_handler
import polling_health
from polling_health import PollingAwareBot, PollingMonitor, PollingSettings, observed_polling_error

POLLING_CONFLICT = 'Conflict: terminated by other getUpdates request; make sure that only one bot instance is running'
WEBHOOK_ACTIVE = (
    "Conflict: can't use getUpdates method while webhook is active; use deleteWebhook to delete the webhook first"
)
WEBHOOK_CHANGED = 'Conflict: terminated by setWebhook request'


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class ConflictTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.send = AsyncMock()
        self.app = SimpleNamespace(updater=SimpleNamespace(running=True), bot=SimpleNamespace(send_message=self.send))
        self.monitor = PollingMonitor(PollingSettings(), '1, 2,1, ,', True, clock=self.clock)
        self.monitor.start(self.app)
        self.addAsyncCleanup(self.monitor.close)
        for name, value in [('send_error_to_admin', True), ('admins_chat_ids', '1,2')]:
            setting = patch.object(general_error_handler, name, value)
            setting.start()
            self.addCleanup(setting.stop)

    async def make_bot(self, outcomes):
        outcomes = iter(outcomes)

        async def handle(request):
            if request.url.path.endswith('/getMe'):
                return httpx.Response(
                    200, json={'ok': True, 'result': {'id': 123, 'is_bot': True, 'first_name': 'Unit'}}
                )
            when, value = next(outcomes)
            self.clock.now = when
            if isinstance(value, BaseException):
                raise value
            if isinstance(value, str):
                return httpx.Response(409, json={'ok': False, 'error_code': 409, 'description': value})
            return httpx.Response(200, json={'ok': True, 'result': value})

        def request():
            return HTTPXRequest(httpx_kwargs={'transport': httpx.MockTransport(handle)})

        bot = PollingAwareBot(
            '123:dummy', request=request(), get_updates_request=request(), polling_observer=self.monitor
        )
        await bot.initialize()
        self.addAsyncCleanup(bot.shutdown)
        return bot

    async def poll(self, bot):
        try:
            return await bot.get_updates(timeout=0)
        except TelegramError as exc:
            await general_error_handler.error_handler(None, SimpleNamespace(error=exc, bot=self.app.bot))
            return exc

    async def test_wilson_sequence_logs_all_failures_without_admin_sends(self):
        bot = await self.make_bot(
            [
                (0, httpx.ReadTimeout('PRIVATE_SENTINEL')),
                (5, httpx.ConnectTimeout('PRIVATE_SENTINEL')),
                (10, httpx.ConnectTimeout('PRIVATE_SENTINEL')),
                (13, POLLING_CONFLICT),
                (24, []),
            ]
        )
        with self.assertLogs('logger', level='INFO') as logs:
            for _ in range(3):
                await self.poll(bot)
            self.assertEqual(self.monitor.incidents.active.failures, 3)
            conflict = await self.poll(bot)
            await self.poll(bot)
        self.send.assert_not_awaited()
        self.assertTrue(observed_polling_error(conflict))
        self.assertIsNone(self.monitor.incidents.active)
        self.assertIsNone(self.monitor.sender_task)
        self.assertTrue(all(recipient.pending is None for recipient in self.monitor.recipients.values()))
        failures = [line for line in logs.output if 'Polling failure ' in line or 'Polling conflict ' in line]
        self.assertEqual(len(failures), 4)
        self.assertNotIn('PRIVATE_SENTINEL', '\n'.join(logs.output))

    async def test_recovery_with_update_before_conflict_and_success_does_not_clear_history(self):
        bot = await self.make_bot(
            [
                (0, httpx.ConnectError('PRIVATE_SENTINEL')),
                (11, [{'update_id': 77}]),
                (13, POLLING_CONFLICT),
                (14, []),
                (16, POLLING_CONFLICT),
            ]
        )
        await self.poll(bot)
        self.assertEqual([update.update_id for update in await self.poll(bot)], [77])
        self.assertIsNone(self.monitor.incidents.active)
        first = await self.poll(bot)
        self.send.assert_not_awaited()
        await self.poll(bot)
        second = await self.poll(bot)
        self.assertTrue(observed_polling_error(first))
        self.assertFalse(observed_polling_error(second))
        self.assertEqual(self.send.await_count, 2)
        self.assertIsNone(self.monitor.sender_task)

    async def test_isolated_and_repeat_boundary_from_clock_zero(self):
        for gap in (119.999, 120, 120.001):
            with self.subTest(gap=gap):
                self.monitor.start(self.app)
                self.send.reset_mock()
                bot = await self.make_bot([(0, POLLING_CONFLICT), (gap, POLLING_CONFLICT)])
                with self.assertLogs('logger', level='WARNING') as logs:
                    first = await self.poll(bot)
                    second = await self.poll(bot)
                self.assertTrue(observed_polling_error(first))
                self.assertIn('WARNING', logs.output[0])
                self.assertEqual(len(logs.output), 2)
                self.assertEqual(self.send.await_count, 2 if gap <= 120 else 0)
                self.assertEqual(observed_polling_error(second), gap > 120)
                self.assertIn('ERROR' if gap <= 120 else 'WARNING', logs.output[1])
                self.assertIsNone(self.monitor.incidents.active)

    async def test_conflict_hint_is_localized_and_only_for_admitted_polling_repeats(self):
        for language, hint in [('en', 'Another bot instance'), ('uk', 'ще один екземпляр')]:
            with self.subTest(language=language), patch.dict(os.environ, {'LANGUAGE': language}):
                self.monitor.start(self.app)
                self.send.reset_mock()
                bot = await self.make_bot([(0, POLLING_CONFLICT), (1, POLLING_CONFLICT), (2, WEBHOOK_ACTIVE)])
                await self.poll(bot)
                self.send.assert_not_awaited()
                await self.poll(bot)
                self.assertEqual(self.send.await_count, 2)
                self.assertTrue(all(hint in call.kwargs['text'] for call in self.send.call_args_list))
                self.send.reset_mock()
                await self.poll(bot)
                self.assertEqual(self.send.await_count, 2)
                self.assertTrue(all(hint not in call.kwargs['text'] for call in self.send.call_args_list))

    async def test_webhook_and_unknown_interleaving_do_not_count_or_reset(self):
        descriptions = [WEBHOOK_ACTIVE, WEBHOOK_CHANGED, 'PRIVATE_SENTINEL', POLLING_CONFLICT + ' PRIVATE_SENTINEL']
        bot = await self.make_bot(
            [(0, POLLING_CONFLICT)]
            + [(i + 10, text) for i, text in enumerate(descriptions)]
            + [(120, POLLING_CONFLICT)]
        )
        await self.poll(bot)
        with self.assertLogs('logger', level='ERROR') as logs:
            for _ in descriptions:
                other = await self.poll(bot)
                self.assertFalse(observed_polling_error(other))
                self.assertEqual(self.monitor.last_conflict_at, 0)
            await self.poll(bot)
        self.assertEqual(self.send.await_count, 2 * (len(descriptions) + 1))
        self.assertNotIn('PRIVATE_SENTINEL', '\n'.join(logs.output))
        self.assertTrue(all('PRIVATE_SENTINEL' not in call.kwargs['text'] for call in self.send.call_args_list))

    async def test_cooldown_boundary_and_quiet_gap_do_not_reset_recipient_limits(self):
        times = [0, 1, 200, 201] + list(range(300, 1800, 100)) + [1800.999, 1801]
        bot = await self.make_bot([(when, POLLING_CONFLICT) for when in times])
        first = await self.poll(bot)
        second = await self.poll(bot)
        self.assertTrue(observed_polling_error(first))
        self.assertFalse(observed_polling_error(second))
        for _ in times[2:-1]:
            await self.poll(bot)
        self.assertEqual(self.send.await_count, 2)
        await self.poll(bot)
        self.assertEqual([call.kwargs['chat_id'] for call in self.send.call_args_list], ['1', '2', '1', '2'])
        self.assertIsNone(self.monitor.sender_task)

    async def test_delayed_admin_attempts_overlap_without_shortening_per_admin_interval(self):
        times = []
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(**kwargs):
            times.append((kwargs['chat_id'], self.clock()))
            if kwargs['chat_id'] == '1' and len(times) == 1:
                entered.set()
                await release.wait()

        self.send.side_effect = send
        intervening = list(range(201, 1801, 100))
        bot = await self.make_bot([(when, POLLING_CONFLICT) for when in [0, 1, 101] + intervening + [1801, 1901]])
        await self.poll(bot)
        # Reserve A at t=1, but leave its I/O pending. Another callback can claim B.
        delayed = asyncio.create_task(self.poll(bot))
        await entered.wait()
        await self.poll(bot)
        self.assertEqual(times, [('1', 1), ('2', 101)])
        release.set()
        await delayed
        for _ in intervening:
            await self.poll(bot)
        await self.poll(bot)
        self.assertEqual(times, [('1', 1), ('2', 101), ('1', 1801)])
        await self.poll(bot)
        self.assertEqual(times, [('1', 1), ('2', 101), ('1', 1801), ('2', 1901)])

    async def test_duplicate_ids_and_failed_attempts_consume_cooldown(self):
        with patch.object(general_error_handler, 'admins_chat_ids', '1, 2,1, ,2'):
            self.send.side_effect = [Forbidden('PRIVATE_SENTINEL'), TimeoutError('PRIVATE_SENTINEL')]
            bot = await self.make_bot([(0, POLLING_CONFLICT), (1, POLLING_CONFLICT), (2, POLLING_CONFLICT)])
            await self.poll(bot)
            with self.assertLogs('logger', level='ERROR') as logs:
                await self.poll(bot)
                await self.poll(bot)
            self.assertEqual([call.kwargs['chat_id'] for call in self.send.call_args_list], ['1', '2'])
            self.assertNotIn('PRIVATE_SENTINEL', '\n'.join(logs.output))
            self.assertTrue(all(recipient.pending is None for recipient in self.monitor.recipients.values()))

    async def test_master_off_no_admins_disabled_and_stopped_monitor(self):
        for change in ('notifications', 'recipients', 'settings', 'running'):
            with self.subTest(change=change):
                self.monitor.notifications = change != 'notifications'
                self.monitor.settings = PollingSettings(enabled=change != 'settings')
                self.app.updater.running = change != 'running'
                if change == 'recipients':
                    self.monitor.recipients.clear()
                bot = await self.make_bot([(0, POLLING_CONFLICT), (1, POLLING_CONFLICT)])
                if change in ('settings', 'running'):
                    # An inactive feature preserves the ordinary path.
                    with patch.object(general_error_handler, 'send_error_to_admin', False):
                        first = await self.poll(bot)
                        self.assertFalse(observed_polling_error(first))
                else:
                    first = await self.poll(bot)
                    second = await self.poll(bot)
                    self.assertTrue(observed_polling_error(first))
                    self.assertTrue(observed_polling_error(second))
                self.send.assert_not_awaited()

    async def test_non_polling_conflicts_remain_ordinary_and_do_not_mutate_state(self):
        bot = await self.make_bot([(0, POLLING_CONFLICT)])
        await self.poll(bot)
        with self.assertLogs('logger', level='ERROR'):
            await general_error_handler.error_handler(
                None, SimpleNamespace(error=Conflict(POLLING_CONFLICT), bot=self.app.bot)
            )
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual(self.monitor.last_conflict_at, 0)

    async def test_shutdown_invalidates_admitted_callback_and_fresh_start_history(self):
        bot = await self.make_bot([(0, POLLING_CONFLICT), (1, POLLING_CONFLICT)])
        await self.poll(bot)
        with self.assertRaises(Conflict) as caught:
            await bot.get_updates(timeout=0)
        exception = caught.exception
        self.assertIsNotNone(polling_health.polling_conflict_admission(exception))
        await self.monitor.close()
        await general_error_handler.error_handler(None, SimpleNamespace(error=exception, bot=self.app.bot))
        self.send.assert_not_awaited()
        self.assertIsNone(self.monitor.last_conflict_at)
        self.assertIsNone(self.monitor.sender_task)
        self.monitor.recipients.update({'1': polling_health.Recipient(), '2': polling_health.Recipient()})
        self.monitor.start(self.app)
        await general_error_handler.error_handler(None, SimpleNamespace(error=exception, bot=self.app.bot))
        self.send.assert_not_awaited()

    async def test_observer_failure_clears_partial_admission_without_replacing_error(self):
        bot = await self.make_bot([(0, POLLING_CONFLICT), (1, POLLING_CONFLICT)])
        await self.poll(bot)
        original = self.monitor.observe_failure

        def observe(exception):
            original(exception)
            raise RuntimeError('PRIVATE_SENTINEL')

        with patch.object(self.monitor, 'observe_failure', side_effect=observe), self.assertLogs(
            'logger', level='ERROR'
        ) as logs:
            with self.assertRaises(Conflict) as caught:
                await bot.get_updates(timeout=0)
            self.assertFalse(observed_polling_error(caught.exception))
            self.assertIsNone(polling_health.polling_conflict_admission(caught.exception))
            await general_error_handler.error_handler(None, SimpleNamespace(error=caught.exception, bot=self.app.bot))
        self.assertEqual(self.send.await_count, 2)
        self.assertNotIn('PRIVATE_SENTINEL', '\n'.join(logs.output))

    async def test_independent_monitor_has_fresh_history_and_cancellation_does_not_change_it(self):
        first = await self.make_bot([(0, POLLING_CONFLICT), (1, asyncio.CancelledError())])
        await self.poll(first)
        with self.assertRaises(asyncio.CancelledError):
            await first.get_updates(timeout=0)
        self.assertEqual(self.monitor.last_conflict_at, 0)
        other = PollingMonitor(PollingSettings(), '1,2', True, clock=self.clock)
        other.start(self.app)
        self.addAsyncCleanup(other.close)
        self.assertIsNone(other.last_conflict_at)
        self.assertFalse(other.observe_failure(asyncio.CancelledError()))
        self.assertIsNone(other.last_conflict_at)
        self.assertIsNone(other.sender_task)

    async def test_observer_logging_failure_cannot_replace_original_polling_error(self):
        bot = await self.make_bot([(0, POLLING_CONFLICT), (1, POLLING_CONFLICT), (2, [])])
        await self.poll(bot)
        with patch.object(polling_health, 'error', side_effect=RuntimeError('PRIVATE_SENTINEL')):
            with self.assertRaises(Conflict) as caught:
                await bot.get_updates(timeout=0)
            with patch.object(self.monitor, 'observe_success', side_effect=RuntimeError('PRIVATE_SENTINEL')):
                self.assertEqual(await bot.get_updates(timeout=0), ())
        self.assertFalse(observed_polling_error(caught.exception))
        self.assertIsNone(polling_health.polling_conflict_admission(caught.exception))

    async def test_master_handler_switch_does_not_spend_recipient_cooldowns(self):
        bot = await self.make_bot([(0, POLLING_CONFLICT), (1, POLLING_CONFLICT), (2, POLLING_CONFLICT)])
        await self.poll(bot)
        with patch.object(general_error_handler, 'send_error_to_admin', False):
            await self.poll(bot)
        self.assertTrue(all(recipient.next_conflict_attempt is None for recipient in self.monitor.recipients.values()))
        await self.poll(bot)
        self.assertEqual(self.send.await_count, 2)

    async def test_long_transport_summary_is_independent_of_isolated_conflict(self):
        bot = await self.make_bot([(0, httpx.ReadTimeout('PRIVATE_SENTINEL')), (5, POLLING_CONFLICT), (120, [])])
        await self.poll(bot)
        conflict = await self.poll(bot)
        self.assertEqual(polling_health.compact_diagnostic(conflict), 'Conflict(polling_conflict)')
        await self.poll(bot)
        await self.monitor.sender_task
        self.assertEqual(self.send.await_count, 2)
        self.assertTrue(all('120.0' in call.kwargs['text'] for call in self.send.call_args_list))
        self.assertTrue(all('polling_conflict' not in call.kwargs['text'] for call in self.send.call_args_list))
        self.assertTrue(all(recipient.next_conflict_attempt is None for recipient in self.monitor.recipients.values()))

    async def test_two_real_updaters_compete_and_only_repeat_reaches_admins(self):
        loop = asyncio.get_running_loop()
        pending = None
        next_conflict_at = loop.time()
        started = loop.time()
        timers = []
        notices = []
        failures = []
        first_waiting, notified = asyncio.Event(), asyncio.Event()

        def conflict_response(future):
            if not future.done():
                future.set_result(httpx.Response(409, json={'ok': False, 'description': POLLING_CONFLICT}))

        async def handle(request):
            nonlocal pending, next_conflict_at
            if request.url.path.endswith('/getMe'):
                return httpx.Response(
                    200, json={'ok': True, 'result': {'id': 123, 'is_bot': True, 'first_name': 'Unit'}}
                )
            parameters = parse_qs(request.content.decode())
            if request.url.path.endswith('/sendMessage'):
                admin_id = parameters['chat_id'][0]
                notices.append((admin_id, loop.time() - started, parameters['text'][0]))
                if len(notices) >= 2:
                    notified.set()
                return httpx.Response(
                    200,
                    json={
                        'ok': True,
                        'result': {
                            'message_id': len(notices),
                            'date': 0,
                            'chat': {'id': int(admin_id), 'type': 'private'},
                        },
                    },
                )
            if not request.url.path.endswith('/getUpdates'):
                return httpx.Response(200, json={'ok': True, 'result': True})
            timeout = float(parameters.get('timeout', ['0'])[0])
            if not timeout:
                return httpx.Response(200, json={'ok': True, 'result': []})
            # Model the relevant Client.cpp semantics: replace the older waiting poll,
            # return 409 to it, and space modeled conflict replies by at least 3 s.
            if pending is not None and not pending.done():
                due = max(loop.time(), next_conflict_at)
                next_conflict_at = due + 3
                timers.append(loop.call_at(due, conflict_response, pending))
            future = loop.create_future()
            pending = future
            first_waiting.set()
            try:
                async with asyncio.timeout(timeout):
                    return await future
            except TimeoutError:
                return httpx.Response(200, json={'ok': True, 'result': []})
            finally:
                if pending is future:
                    pending = None

        def bot(observer):
            def request():
                return HTTPXRequest(httpx_kwargs={'transport': httpx.MockTransport(handle)})

            return PollingAwareBot(
                '123:dummy', request=request(), get_updates_request=request(), polling_observer=observer
            )

        # Only the watched application has the production alert handler. Its competitor
        # is a real PTB updater without admin alerts, not a second production process.
        competitor_observer = SimpleNamespace(observe_failure=lambda _exc: False, observe_success=lambda: None)
        apps = [Application.builder().bot(bot(observer)).build() for observer in (self.monitor, competitor_observer)]
        watched = apps[0]
        self.monitor.clock = lambda: loop.time() - started
        self.monitor.start(watched)

        async def on_error(_update, context):
            failures.append(context.error)
            await general_error_handler.error_handler(None, context)

        watched.add_error_handler(on_error)

        def callback(exception):
            watched.create_task(watched.process_error(update=None, error=exception))

        try:
            for app in apps:
                await app.initialize()
                await app.start()
            await watched.updater.start_polling(timeout=10, error_callback=callback)
            await asyncio.wait_for(first_waiting.wait(), 5)
            await apps[1].updater.start_polling(timeout=10, error_callback=lambda _exc: None)
            await asyncio.wait_for(notified.wait(), 30)
            self.assertGreaterEqual(len(failures), 2)
            self.assertTrue(observed_polling_error(failures[0]))
            self.assertFalse(observed_polling_error(failures[1]))
            self.assertEqual([notice[0] for notice in notices], ['1', '2'])
            self.assertTrue(all('telegram_error=polling_conflict' in notice[2] for notice in notices))
            self.assertIsNone(self.monitor.sender_task)
        finally:
            for app in reversed(apps):
                if app.updater.running:
                    await app.updater.stop()
                if app.running:
                    await app.stop()
                await app.shutdown()
            for timer in timers:
                timer.cancel()


if __name__ == '__main__':
    unittest.main()
