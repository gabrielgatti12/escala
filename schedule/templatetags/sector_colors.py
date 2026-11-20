from django import template
from schedule.presentation import sector_hue, sector_label, activity_style
register=template.Library()
register.filter('sector_hue',sector_hue)
register.filter('sector_label',sector_label)
register.filter('activity_style',activity_style)
