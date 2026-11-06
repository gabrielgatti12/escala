"""Operational timestamps for the Sheets worker, independent of imported content."""
import time
from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from .models import DriveFolder, SheetSource, SyncHeartbeat, SyncState


WORKER_KEY = 'sheets'
CONTACT_GRACE = timedelta(minutes=10)


def begin_cycle(now=None):
    now = now or timezone.now()
    state, _ = SyncHeartbeat.objects.update_or_create(
        key=WORKER_KEY,
        defaults={
            'started_at': now,
            'heartbeat_at': now,
            'next_run_at': None,
            'running': True,
            'total_sources': 0,
            'processed_sources': 0,
            'failed_sources': 0,
            'discovery_failed': False,
            'refresh_requested_at': None,
        },
    )
    return state


def touch_cycle(now=None):
    now = now or timezone.now()
    SyncHeartbeat.objects.filter(key=WORKER_KEY).update(heartbeat_at=now)


def set_cycle_sources(total, now=None):
    now = now or timezone.now()
    SyncHeartbeat.objects.filter(key=WORKER_KEY).update(
        total_sources=max(0, total), heartbeat_at=now,
    )


def record_source_result(failed=False, now=None):
    now = now or timezone.now()
    SyncHeartbeat.objects.filter(key=WORKER_KEY).update(
        heartbeat_at=now,
        processed_sources=F('processed_sources') + 1,
        failed_sources=F('failed_sources') + int(bool(failed)),
    )


def finish_cycle(interval, discovery_failed=False, now=None):
    now = now or timezone.now()
    interval = max(0, int(interval))
    SyncHeartbeat.objects.filter(key=WORKER_KEY).update(
        running=False,
        heartbeat_at=now,
        finished_at=now,
        next_run_at=now + timedelta(seconds=interval) if interval else None,
        discovery_failed=discovery_failed,
    )


def request_refresh(now=None):
    """Queue one manual refresh without starting a second Sheets worker."""
    now = now or timezone.now()
    SyncHeartbeat.objects.get_or_create(key=WORKER_KEY)
    return bool(SyncHeartbeat.objects.filter(
        key=WORKER_KEY, refresh_requested_at__isnull=True,
    ).update(refresh_requested_at=now))


def wait_for_next_cycle(interval, poll_seconds=30, maintenance=None):
    """Wait using local SQLite only, waking early for an administrator request.

    A monotonic deadline tolerates clock corrections. A queued request remains
    in the database until begin_cycle consumes it, including if this process is
    stopped while waiting. Polls are short so process shutdown remains prompt.
    Optional local maintenance runs at most once per five minutes; callers must
    keep it confined to SQLite so reminders never trigger another Google read.
    """
    interval = max(0, int(interval))
    poll_seconds = max(1, min(30, float(poll_seconds)))
    started = time.monotonic()
    deadline = started + interval
    next_maintenance = started + 300
    while True:
        touch_cycle()
        if SyncHeartbeat.objects.filter(
            key=WORKER_KEY, refresh_requested_at__isnull=False,
        ).exists():
            return True
        current = time.monotonic()
        remaining = deadline - current
        if remaining <= 0:
            return False
        if maintenance and current >= next_maintenance:
            maintenance()
            next_maintenance = current + 300
        sleep_for = min(poll_seconds, remaining)
        if maintenance:
            sleep_for = min(sleep_for, next_maintenance - current)
        time.sleep(sleep_for)


def interval_label(interval):
    interval = max(0, int(interval))
    if not interval:
        return 'execução única'
    for seconds, singular, plural in [(3600, 'hora', 'horas'), (60, 'minuto', 'minutos'), (1, 'segundo', 'segundos')]:
        if interval % seconds == 0:
            value = interval // seconds
            return f'{value} {singular if value == 1 else plural}'


def sync_health(now=None, interval=86400):
    """Report the worker and *all* enabled sources, without changing any data.

    Waiting until the next daily cycle is normal. A source can legitimately be
    older than the interval: every pass reads all sources before its pause.
    A live pass uses a conservative per-source download budget while its full
    duration is not yet known; a completed pass uses its measured duration.
    """
    now = now or timezone.now()
    interval = max(0, int(interval))
    heartbeat = SyncHeartbeat.objects.filter(key=WORKER_KEY).first()
    sources = list(SheetSource.objects.filter(enabled=True).values('spreadsheet_id', 'error'))
    successes = dict(SyncState.objects.filter(
        source__in=[source['spreadsheet_id'] for source in sources],
    ).values_list('source', 'last_success'))
    completed = [successes[source['spreadsheet_id']] for source in sources
                 if successes.get(source['spreadsheet_id'])]
    source_error_count = sum(bool(source['error']) for source in sources)
    folder_error_count = DriveFolder.objects.filter(enabled=True).exclude(error='').count()
    folder_count = DriveFolder.objects.filter(enabled=True).count()
    never_imported_count = len(sources) - len(completed)

    cycle_duration = timedelta(0)
    if heartbeat and heartbeat.started_at:
        if heartbeat.running:
            cycle_duration = max(
                now - heartbeat.started_at,
                timedelta(seconds=45 * len(sources)),
            )
        elif heartbeat.finished_at:
            cycle_duration = max(heartbeat.finished_at - heartbeat.started_at, timedelta(0))
    expected_max_age = max(
        timedelta(minutes=15),
        cycle_duration + timedelta(seconds=interval) + CONTACT_GRACE,
    )
    stale_source_count = sum(now - success > expected_max_age for success in completed)
    last_contact = heartbeat.heartbeat_at if heartbeat else None
    next_attempt = heartbeat.next_run_at if heartbeat else None
    manual_pending = bool(heartbeat and heartbeat.refresh_requested_at)
    has_failures = bool(source_error_count or folder_error_count or
                        (heartbeat and (heartbeat.failed_sources or heartbeat.discovery_failed)))

    if not heartbeat or (not heartbeat.started_at and not last_contact):
        status = 'unknown'
        headline = 'Atualização solicitada; o serviço ainda não registrou uma execução.' if manual_pending else 'O serviço ainda não registrou uma execução.'
    elif not last_contact or now - last_contact > CONTACT_GRACE:
        status = 'stale'
        headline = 'O sincronizador está sem contato recente.'
    elif not heartbeat.running and next_attempt and now - next_attempt > CONTACT_GRACE:
        status = 'stale'
        headline = 'Não houve uma nova execução no prazo esperado.'
    elif heartbeat.running:
        status = 'running'
        headline = 'Sincronização em andamento.'
    elif has_failures:
        status = 'error'
        headline = 'Há falhas de sincronização para conferir.'
    elif never_imported_count:
        status = 'error'
        headline = 'Há fontes aguardando a primeira importação.'
    elif stale_source_count:
        status = 'stale'
        headline = 'Há planilhas sem atualização recente.'
    elif not sources and not folder_count:
        status = 'unknown'
        headline = 'Nenhuma fonte ativa está configurada.'
    elif manual_pending:
        status = 'waiting'
        headline = 'Atualização manual solicitada; aguardando o sincronizador.'
    else:
        status = 'waiting'
        headline = 'Último ciclo concluído; aguardando a próxima tentativa.'

    return {
        'status': status,
        'headline': headline,
        'running': bool(heartbeat and heartbeat.running),
        'started_at': heartbeat.started_at if heartbeat else None,
        'finished_at': heartbeat.finished_at if heartbeat else None,
        'last_contact': last_contact,
        'next_attempt': next_attempt,
        'manual_pending': manual_pending,
        'refresh_requested_at': heartbeat.refresh_requested_at if heartbeat else None,
        'interval_label': interval_label(interval),
        'total_sources': len(sources),
        'cycle_total_sources': heartbeat.total_sources if heartbeat else 0,
        'processed_sources': heartbeat.processed_sources if heartbeat else 0,
        'failed_sources': heartbeat.failed_sources if heartbeat else 0,
        'discovery_failed': bool(heartbeat and heartbeat.discovery_failed),
        'source_error_count': source_error_count,
        'folder_error_count': folder_error_count,
        'never_imported_count': never_imported_count,
        'stale_source_count': stale_source_count,
        'oldest_success': min(completed) if completed else None,
        'latest_success': max(completed) if completed else None,
        'all_sources_recent': bool(sources) and not (
            has_failures or never_imported_count or stale_source_count or status in ['stale', 'unknown']
        ),
        'expected_max_age_seconds': int(expected_max_age.total_seconds()),
    }
