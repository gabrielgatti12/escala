from django import template
from schedule.presentation import sector_hue, sector_label
register=template.Library()
register.filter('sector_hue',sector_hue)
register.filter('sector_label',sector_label)
