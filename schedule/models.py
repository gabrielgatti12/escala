from django.conf import settings
from django.db import models
from django.utils import timezone
from urllib.parse import quote
class Shift(models.Model):
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
        return f"https://docs.google.com/spreadsheets/d/{self.source}/edit?range="+quote("'Espelho Semanal'!"+self.source_cell) if self.source and self.source_cell else ''
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
    source = models.CharField(max_length=100,unique=True)
    last_success = models.DateTimeField(null=True,blank=True)
    summary = models.CharField(max_length=250,blank=True)

class SharedActivity(models.Model):
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
        return f"https://docs.google.com/spreadsheets/d/{self.source}/edit?range="+quote("'Espelho Semanal'!"+self.source_cell) if self.source and self.source_cell else ''
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

class ResidentAccess(models.Model):
    account = models.OneToOneField(settings.AUTH_USER_MODEL,on_delete=models.CASCADE,related_name='resident_access')
    resident = models.OneToOneField(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,related_name='access_account')
    def __str__(self):
        return f'{self.account} → {self.resident.get_full_name() or self.resident.username}'

class IncompleteActivity(models.Model):
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
        return f"https://docs.google.com/spreadsheets/d/{self.source}/edit?range="+quote("'Espelho Semanal'!"+self.source_cell)

class DriveFolder(models.Model):
    folder_id = models.CharField(max_length=100,unique=True)
    name = models.CharField(max_length=200,blank=True)
    enabled = models.BooleanField(default=True)
    last_success = models.DateTimeField(null=True,blank=True)
    error = models.CharField(max_length=250,blank=True)
    def __str__(self): return self.name or self.folder_id

class SheetSource(models.Model):
    spreadsheet_id = models.CharField(max_length=100,unique=True)
    name = models.CharField(max_length=200,blank=True)
    folder = models.ForeignKey(DriveFolder,null=True,blank=True,on_delete=models.SET_NULL)
    enabled = models.BooleanField(default=True)
    error = models.CharField(max_length=250,blank=True)
    def __str__(self): return self.name or self.spreadsheet_id
