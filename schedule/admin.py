from django.contrib import admin
from .models import Shift,Exchange
@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ['external_id','resident','starts_at','ends_at','sector']
    list_filter = ['sector','resident']
@admin.register(Exchange)
class ExchangeAdmin(admin.ModelAdmin):
    list_display = ['proposer','recipient','offered','requested','status','created_at']
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False

from .models import SharedActivity,RotationActivity,SyncState
@admin.register(SharedActivity)
class SharedActivityAdmin(admin.ModelAdmin):
    list_display=['sector','starts_at','ends_at']
    filter_horizontal=['residents']
admin.site.register(RotationActivity)
admin.site.register(SyncState)

from .models import Allocation
@admin.register(Allocation)
class AllocationAdmin(admin.ModelAdmin):
    list_display=['proposer','recipient','mine','theirs','status']
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False

from .models import ResidentAccess
@admin.register(ResidentAccess)
class ResidentAccessAdmin(admin.ModelAdmin):
    list_display=['account','resident']
    autocomplete_fields=['account','resident']

from .models import DriveFolder,SheetSource,IncompleteActivity
@admin.register(DriveFolder)
class DriveFolderAdmin(admin.ModelAdmin):
    list_display=['name','folder_id','enabled','last_success','error']
    readonly_fields=['last_success','error']
@admin.register(SheetSource)
class SheetSourceAdmin(admin.ModelAdmin):
    list_display=['name','spreadsheet_id','folder','enabled','error']
    readonly_fields=['error']
    list_filter=['enabled','folder']
@admin.register(IncompleteActivity)
class IncompleteActivityAdmin(admin.ModelAdmin):
    list_display=['date','sector','source_cell','raw_time']
