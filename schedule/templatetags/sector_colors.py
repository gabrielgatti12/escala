from django import template
from schedule.presentation import sector_hue
register=template.Library()
register.filter('sector_hue',sector_hue)
