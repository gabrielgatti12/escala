from django.conf import settings
from django.db import models
from django.utils import timezone
from urllib.parse import quote
import re
import unicodedata

def is_rest_sector(value):
    value=unicodedata.normalize("NFKD",value).encode("ascii","ignore").decode().lower()
    return "pos plantao" in re.sub(r"[^a-z0-9]+"," ",value)

class Shift(models.Model):
    category = models.CharField(max_length=20, choices=[('regular','Atividade'),('cinderela','Cinderela'),('night','Noturno / 24h'),('weekend','Diurno / fim de semana')], default='regular')
    resident_group = models.CharField(max_length=20, blank=True)
    source_tab = models.CharField(max_length=100, default='Espelho Semanal')
    source_snapshot = models.JSONField(default=dict)
    requires_reconciliation = models.BooleanField(default=False)
    external_id = models.CharField(max_length=120, unique=True)
    wants_exchange = models.BooleanField(default=False)
    source = models.CharField(max_length=100,blank=True)
    source_cell = models.CharField(max_length=30,blank=True)
    active = models.BooleanField(default=True)
    resident = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    sector = models.CharField(max_length=120)
    class Meta:
        ordering = ['starts_at']
        constraints = [models.CheckConstraint(condition=models.Q(ends_at__gt=models.F('starts_at')),name='positive_shift_duration')]
    @property
    def source_url(self):
        return f"https://docs.google.com/spreadsheets/d/{self.source}/edit?range="+quote("'"+self.source_tab.replace("'","''")+"'!"+self.source_cell) if self.source and self.source_cell else ''
    def __str__(self):
        return f'{self.resident.get_full_name() or self.resident.username} · {timezone.localtime(self.starts_at):%d/%m %H:%M} · {self.sector}'
class Exchange(models.Model):
    offered = models.ForeignKey(Shift,on_delete=models.PROTECT,related_name='offers')
    requested = models.ForeignKey(Shift,on_delete=models.PROTECT,related_name='requests')
    proposer = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='proposals')
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='received_proposals')
    status = models.CharField(max_length=12,choices=[('pending','Aguardando confirmação'),('accepted','Aceita'),('rejected','Recusada'),('cancelled','Cancelada')],default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True,blank=True)
    @property
    def is_expired(self):
        return self.status=='pending' and min(self.offered.starts_at,self.requested.starts_at)<=timezone.now()
    class Meta:
        ordering = ['-created_at']

class RotationActivity(models.Model):
    external_id = models.CharField(max_length=120,unique=True)
    source = models.CharField(max_length=100)
    resident = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    date = models.DateField()
    period = models.CharField(max_length=30)
    activity = models.CharField(max_length=250)
    group = models.CharField(max_length=30)
    class Meta:
        ordering = ['date','period','resident__username']

class SyncState(models.Model):
    @property
    def reconciliation_count(self):
        match = re.search(r'(\d+) exigem conciliação', self.summary)
        return int(match.group(1)) if match else 0
    source = models.CharField(max_length=100,unique=True)
    last_success = models.DateTimeField(null=True,blank=True)
    summary = models.CharField(max_length=250,blank=True)

class SharedActivity(models.Model):
    source_tab = models.CharField(max_length=100, default='Espelho Semanal')
    source_cell = models.CharField(max_length=30,blank=True)
    active = models.BooleanField(default=True)
    allocated_to = models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.PROTECT,related_name='allocated_activities')
    external_id = models.CharField(max_length=120,unique=True)
    source = models.CharField(max_length=100)
    residents = models.ManyToManyField(settings.AUTH_USER_MODEL)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    sector = models.CharField(max_length=120)
    @property
    def source_url(self):
        return f"https://docs.google.com/spreadsheets/d/{self.source}/edit?range="+quote("'"+self.source_tab.replace("'","''")+"'!"+self.source_cell) if self.source and self.source_cell else ''
    def __str__(self):
        return f'{timezone.localtime(self.starts_at):%d/%m %H:%M} · {self.sector}'
    class Meta:
        ordering = ['starts_at','sector']

class Allocation(models.Model):
    proposer = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='allocation_proposals')
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='allocation_inbox')
    mine = models.ForeignKey(SharedActivity,on_delete=models.PROTECT,related_name='mine_proposals')
    theirs = models.ForeignKey(SharedActivity,on_delete=models.PROTECT,related_name='their_proposals')
    status = models.CharField(max_length=12,choices=Exchange._meta.get_field('status').choices,default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True,blank=True)
    @property
    def is_expired(self):
        return self.status=='pending' and min(self.mine.starts_at,self.theirs.starts_at)<=timezone.now()

class ResidentAccess(models.Model):
    account = models.OneToOneField(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name='resident_access')
    resident = models.OneToOneField(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='access_account')
    def __str__(self):
        return f'{self.account} → {self.resident.get_full_name() or self.resident.username}'

class IncompleteActivity(models.Model):
    source_tab = models.CharField(max_length=100, default='Espelho Semanal')
    @property
    def is_rest(self): return is_rest_sector(self.sector)
    external_id = models.CharField(max_length=120,unique=True)
    source = models.CharField(max_length=100)
    source_cell = models.CharField(max_length=30)
    date = models.DateField()
    residents = models.ManyToManyField(settings.AUTH_USER_MODEL)
    sector = models.CharField(max_length=120)
    raw_time = models.CharField(max_length=100,blank=True)
    class Meta:
        ordering=['date','sector']
    @property
    def source_url(self):
        return f"https://docs.google.com/spreadsheets/d/{self.source}/edit?range="+quote("'"+self.source_tab.replace("'","''")+"'!"+self.source_cell)

class DriveFolder(models.Model):
    folder_id = models.CharField(max_length=100,unique=True)
    name = models.CharField(max_length=200,blank=True)
    enabled = models.BooleanField(default=True)
    last_success = models.DateTimeField(null=True,blank=True)
    error = models.CharField(max_length=250,blank=True)
    def __str__(self): return self.name or self.folder_id

class SheetSource(models.Model):
    format = models.CharField(max_length=20, choices=[('weekly','Escala semanal'),('cinderelas','Cinderelas 18–24'),('plantoes','Plantões completos')], default='weekly')
    warnings = models.TextField(blank=True)
    spreadsheet_id = models.CharField(max_length=100,unique=True)
    name = models.CharField(max_length=200,blank=True)
    folder = models.ForeignKey(DriveFolder,null=True,blank=True,on_delete=models.SET_NULL)
    enabled = models.BooleanField(default=True)
    error = models.CharField(max_length=250,blank=True)
    def __str__(self): return self.name or self.spreadsheet_id

class ExchangeTopic(models.Model):
    shift = models.ForeignKey(Shift,on_delete=models.PROTECT,related_name='exchange_topics')
    poster = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='forum_topics')
    published_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True,blank=True)
    class Meta:
        ordering=['-published_at','-pk']
        constraints=[models.UniqueConstraint(fields=['shift'],condition=models.Q(closed_at__isnull=True),name='one_open_exchange_topic_per_shift')]
    @property
    def is_open(self):
        return self.closed_at is None and self.shift.active and self.shift.wants_exchange and self.shift.resident_id==self.poster_id and self.shift.starts_at>timezone.now()

class ForumComment(models.Model):
    topic = models.ForeignKey(ExchangeTopic,on_delete=models.CASCADE,related_name='comments')
    author = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='forum_comments')
    text = models.CharField(max_length=600)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering=['created_at','pk']


class CalendarSubscription(models.Model):
    resident = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='calendar_subscription')
    account = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name='calendar_subscriptions')
    token_digest = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    rotated_at = models.DateTimeField(auto_now=True)


class Notification(models.Model):
    resident = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='schedule_notifications')
    kind = models.CharField(max_length=20, choices=[('schedule', 'Alteração de escala'), ('request', 'Solicitação recebida'), ('reminder', 'Lembrete')])
    title = models.CharField(max_length=160)
    message = models.TextField()
    payload = models.JSONField(default=dict)
    link = models.CharField(max_length=500, blank=True)
    dedup_key = models.CharField(max_length=160, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    @property
    def display_at(self):
        from django.utils.dateparse import parse_datetime
        value = self.payload.get('last_reminded_at') if isinstance(self.payload, dict) else None
        try:
            latest = parse_datetime(value) if isinstance(value, str) else None
        except ValueError:
            latest = None
        return latest if latest and timezone.is_aware(latest) else self.created_at

    class Meta:
        ordering = ['-created_at', '-pk']
        indexes = [models.Index(fields=['resident', 'read_at'], name='notification_resident_unread')]


class SyncHeartbeat(models.Model):
    key = models.CharField(max_length=30, primary_key=True, default='sheets')
    started_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)
    running = models.BooleanField(default=False)
    total_sources = models.PositiveIntegerField(default=0)
    processed_sources = models.PositiveIntegerField(default=0)
    failed_sources = models.PositiveIntegerField(default=0)
    discovery_failed = models.BooleanField(default=False)
    refresh_requested_at = models.DateTimeField(null=True, blank=True)


class LoginAttempt(models.Model):
    # Keyed HMACs only: no login names, passwords, or IP addresses are stored.
    key = models.CharField(max_length=64, primary_key=True)
    window_started = models.DateTimeField()
    failures = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=['window_started'], name='login_attempt_expiry')]
