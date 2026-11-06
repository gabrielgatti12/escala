from datetime import timedelta
from unittest.mock import call, patch

from django.test import TestCase
from django.utils import timezone

from .models import DriveFolder, SheetSource, SyncHeartbeat, SyncState
from .monitoring import begin_cycle, finish_cycle, record_source_result, request_refresh, set_cycle_sources, sync_health, touch_cycle, wait_for_next_cycle


class SyncHealthTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)

    def source(self, key, success=None, error='', enabled=True):
        source = SheetSource.objects.create(spreadsheet_id=key, error=error, enabled=enabled)
        if success is not None:
            SyncState.objects.create(source=key, last_success=success)
        return source

    def test_no_heartbeat_does_not_claim_a_running_service(self):
        self.source('recent-source', self.now)
        health = sync_health(self.now)
        self.assertEqual(health['status'], 'unknown')
        self.assertFalse(health['all_sources_recent'])
        self.assertIsNone(health['last_contact'])
        self.assertEqual(health['latest_success'], self.now)

    def test_progress_and_next_attempt_follow_complete_cycle_then_pause(self):
        self.source('first', self.now)
        self.source('second', self.now + timedelta(seconds=45))
        begin_cycle(self.now)
        set_cycle_sources(2, self.now)
        record_source_result(now=self.now + timedelta(seconds=5))
        record_source_result(failed=True, now=self.now + timedelta(seconds=45))
        finish_cycle(300, now=self.now + timedelta(seconds=45))
        health = sync_health(self.now + timedelta(seconds=60))
        self.assertEqual(health['processed_sources'], 2)
        self.assertEqual(health['failed_sources'], 1)
        self.assertEqual(health['next_attempt'], self.now + timedelta(seconds=345))
        self.assertEqual(health['status'], 'error')
        self.assertFalse(health['running'])
        self.assertFalse(health['all_sources_recent'])

    def test_normal_wait_after_a_long_pass_does_not_mark_52_sources_stale(self):
        start = self.now - timedelta(minutes=40)
        for number in range(52):
            self.source(str(number), start + timedelta(seconds=45 * number))
        begin_cycle(start)
        set_cycle_sources(52, start)
        finish_cycle(300, now=self.now)
        health = sync_health(self.now + timedelta(minutes=4))
        self.assertEqual(health['status'], 'waiting')
        self.assertEqual(health['stale_source_count'], 0)
        self.assertTrue(health['all_sources_recent'])
        self.assertEqual(health['oldest_success'], start)
        self.assertEqual(health['latest_success'], start + timedelta(seconds=45 * 51))

    def test_running_pass_is_not_stale_just_because_all_sources_take_time(self):
        start = self.now - timedelta(minutes=20)
        for number in range(52):
            self.source(str(number), self.now - timedelta(minutes=35))
        begin_cycle(start)
        set_cycle_sources(52, start)
        touch_cycle(self.now)
        health = sync_health(self.now)
        self.assertEqual(health['status'], 'running')
        self.assertEqual(health['stale_source_count'], 0)

    def test_latest_success_does_not_hide_missing_old_or_failed_sources(self):
        self.source('recent', self.now)
        self.source('old', self.now - timedelta(days=2), error='Leitura falhou.')
        self.source('new')
        self.source('disabled', self.now - timedelta(days=5), error='Pausada', enabled=False)
        begin_cycle(self.now - timedelta(seconds=30))
        set_cycle_sources(3, self.now)
        finish_cycle(300, now=self.now)
        health = sync_health(self.now)
        self.assertEqual(health['latest_success'], self.now)
        self.assertEqual(health['never_imported_count'], 1)
        self.assertEqual(health['stale_source_count'], 1)
        self.assertEqual(health['source_error_count'], 1)
        self.assertEqual(health['total_sources'], 3)
        self.assertEqual(health['status'], 'error')
        self.assertFalse(health['all_sources_recent'])

    def test_running_without_heartbeat_for_ten_minutes_requires_attention(self):
        begin_cycle(self.now - timedelta(minutes=11))
        health = sync_health(self.now)
        self.assertEqual(health['status'], 'stale')
        self.assertTrue(health['running'])

    def test_next_attempt_not_due_is_a_normal_pause_even_with_large_interval(self):
        self.source('one', self.now)
        begin_cycle(self.now - timedelta(seconds=1))
        finish_cycle(3600, now=self.now)
        touch_cycle(self.now + timedelta(minutes=20))
        health = sync_health(self.now + timedelta(minutes=20), interval=3600)
        self.assertEqual(health['status'], 'waiting')

    def test_daily_pause_with_recent_heartbeat_is_healthy_after_twenty_hours(self):
        self.source('one', self.now)
        begin_cycle(self.now - timedelta(seconds=1))
        finish_cycle(86400, now=self.now)
        touch_cycle(self.now + timedelta(hours=20))
        health = sync_health(self.now + timedelta(hours=20))
        self.assertEqual(health['status'], 'waiting')
        self.assertEqual(health['interval_label'], '24 horas')
        self.assertEqual(health['next_attempt'], self.now + timedelta(days=1))
        self.assertEqual(health['stale_source_count'], 0)
        self.assertTrue(health['all_sources_recent'])

    def test_daily_pause_without_recent_heartbeat_is_not_reported_healthy(self):
        self.source('one', self.now)
        begin_cycle(self.now - timedelta(seconds=1))
        finish_cycle(86400, now=self.now)
        health = sync_health(self.now + timedelta(minutes=15))
        self.assertEqual(health['status'], 'stale')
        self.assertGreater(health['next_attempt'], self.now + timedelta(minutes=15))
        self.assertFalse(health['all_sources_recent'])

    def test_missing_next_cycle_is_reported_as_stale(self):
        self.source('one', self.now)
        begin_cycle(self.now - timedelta(seconds=1))
        finish_cycle(300, now=self.now)
        health = sync_health(self.now + timedelta(minutes=16))
        self.assertEqual(health['status'], 'stale')
        self.assertFalse(health['all_sources_recent'])

    def test_discovery_failure_is_not_hidden_by_successful_known_sources(self):
        self.source('one', self.now)
        DriveFolder.objects.create(folder_id='folder', error='Acesso Drive indisponível.')
        begin_cycle(self.now - timedelta(seconds=1))
        finish_cycle(300, discovery_failed=True, now=self.now)
        health = sync_health(self.now)
        self.assertEqual(health['status'], 'error')
        self.assertTrue(health['discovery_failed'])
        self.assertEqual(health['folder_error_count'], 1)
        self.assertFalse(health['all_sources_recent'])

    def test_new_cycle_resets_progress_without_erasing_last_complete_time(self):
        begin_cycle(self.now - timedelta(minutes=10))
        set_cycle_sources(5, self.now)
        record_source_result(failed=True, now=self.now)
        finish_cycle(300, discovery_failed=True, now=self.now)
        begin_cycle(self.now + timedelta(minutes=5))
        heartbeat = SyncHeartbeat.objects.get()
        self.assertEqual(heartbeat.finished_at, self.now)
        self.assertTrue(heartbeat.running)
        self.assertEqual(heartbeat.processed_sources, 0)
        self.assertEqual(heartbeat.failed_sources, 0)
        self.assertFalse(heartbeat.discovery_failed)
        self.assertIsNone(heartbeat.next_run_at)

    def test_health_read_never_creates_or_changes_worker_records(self):
        self.source('one')
        sync_health(self.now)
        self.assertFalse(SyncHeartbeat.objects.exists())

    def test_manual_request_is_idempotent_and_is_consumed_only_when_cycle_begins(self):
        self.assertTrue(request_refresh(self.now))
        self.assertFalse(request_refresh(self.now + timedelta(seconds=10)))
        heartbeat = SyncHeartbeat.objects.get()
        self.assertEqual(heartbeat.refresh_requested_at, self.now)
        health = sync_health(self.now)
        self.assertTrue(health['manual_pending'])
        self.assertEqual(health['status'], 'unknown')
        begin_cycle(self.now + timedelta(seconds=30))
        heartbeat.refresh_from_db()
        self.assertIsNone(heartbeat.refresh_requested_at)
        self.assertFalse(sync_health(self.now + timedelta(seconds=30))['manual_pending'])

    def test_manual_request_during_current_cycle_is_kept_for_following_cycle(self):
        begin_cycle(self.now)
        self.assertTrue(request_refresh(self.now + timedelta(seconds=5)))
        finish_cycle(86400, now=self.now + timedelta(seconds=10))
        self.assertTrue(sync_health(self.now + timedelta(seconds=10))['manual_pending'])
        begin_cycle(self.now + timedelta(seconds=30))
        self.assertIsNone(SyncHeartbeat.objects.get().refresh_requested_at)

    def test_manual_request_wakes_daily_wait_on_next_local_poll(self):
        begin_cycle(self.now)
        finish_cycle(86400, now=self.now)
        with patch('schedule.monitoring.time.monotonic', side_effect=[0, 0]), patch(
            'schedule.monitoring.time.sleep', side_effect=lambda seconds: request_refresh(self.now),
        ) as sleep:
            self.assertTrue(wait_for_next_cycle(86400))
        sleep.assert_called_once_with(30)
        self.assertEqual(SyncHeartbeat.objects.get().refresh_requested_at, self.now)

    def test_local_wait_keeps_heartbeat_and_limits_sleep_duration(self):
        begin_cycle(self.now - timedelta(hours=1))
        finish_cycle(60, now=self.now - timedelta(hours=1))
        with patch('schedule.monitoring.time.monotonic', side_effect=[0, 0, 30, 60]), patch(
            'schedule.monitoring.time.sleep',
        ) as sleep:
            self.assertFalse(wait_for_next_cycle(60, poll_seconds=120))
        self.assertEqual(sleep.call_args_list, [call(30), call(30)])
        self.assertGreater(SyncHeartbeat.objects.get().heartbeat_at, self.now - timedelta(minutes=1))

    def test_already_queued_request_wakes_without_sleeping(self):
        request_refresh(self.now)
        with patch('schedule.monitoring.time.monotonic', return_value=0), patch(
            'schedule.monitoring.time.sleep',
        ) as sleep:
            self.assertTrue(wait_for_next_cycle(86400))
        sleep.assert_not_called()

    def test_local_maintenance_runs_every_five_minutes_not_every_heartbeat(self):
        begin_cycle(self.now)
        finish_cycle(86400, now=self.now)
        maintenance_calls = []
        with patch('schedule.monitoring.time.monotonic', side_effect=[0] + list(range(0, 661, 30))), patch(
            'schedule.monitoring.time.sleep',
        ) as sleep:
            self.assertFalse(wait_for_next_cycle(
                660, maintenance=lambda: maintenance_calls.append(sleep.call_count),
            ))
        self.assertEqual(maintenance_calls, [10, 20])
        self.assertEqual(sleep.call_count, 22)
        self.assertTrue(all(arguments == call(30) for arguments in sleep.call_args_list))

    def test_short_wait_does_not_run_local_maintenance_early(self):
        begin_cycle(self.now)
        finish_cycle(60, now=self.now)
        with patch('schedule.monitoring.time.monotonic', side_effect=[0, 0, 30, 60]), patch(
            'schedule.monitoring.time.sleep',
        ), patch('schedule.monitoring.touch_cycle') as heartbeat:
            maintenance = []
            self.assertFalse(wait_for_next_cycle(60, maintenance=lambda: maintenance.append(True)))
        self.assertEqual(maintenance, [])
        self.assertEqual(heartbeat.call_count, 3)
