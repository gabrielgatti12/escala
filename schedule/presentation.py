"""Stable sector colors and spreadsheet-style grouping, independent of ownership."""
import hashlib
import re
import unicodedata
from functools import lru_cache

def sector_label(label):
    """Display the activity before its location without changing stored keys."""
    location, separator, activity = str(label or '').partition('·')
    if separator and activity.strip() and location.strip().upper() in {'HOSPITAL', 'INSTITUTO', 'UNEX', 'EXTERNO', 'OUTROS'}:
        return f'{activity.strip()} · {location.strip()}'
    return label

def sector_key(label):
    text=unicodedata.normalize('NFKD',label).encode('ascii','ignore').decode().casefold()
    return re.sub(r'\s+',' ',text).strip()

@lru_cache(maxsize=512)
def sector_hue(label):
    named={'instituto  ambulatorio geral (patologia)':18,'instituto  refracao':198,'instituto  usg instituto':148,'instituto  (pr) projeto retina':270,'hospital  sala dr pedro':330,'hospital  sala danilo/julia':48,'instituto  ambulatorio rita l. obeid':95}
    named={sector_key(k):v for k,v in named.items()}
    if sector_key(label) in named:return f'{named[sector_key(label)]:.2f}'
    number=int(hashlib.sha256(sector_key(label).encode()).hexdigest()[:8],16)
    return f'{number / 0xffffffff * 360:.2f}'

_HEX_COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')


def source_color(value):
    """Only a literal six-digit CSS color may enter an inline style."""
    return value.upper() if isinstance(value, str) and _HEX_COLOR.fullmatch(value) else ''


def activity_style(item):
    """Use imported activity colors, retaining stable fallback hues for old data."""
    parts = [f'--sector-hue:{sector_hue(getattr(item, "sector", ""))}']
    for field, variable in [('source_background', '--source-bg'), ('source_foreground', '--source-fg')]:
        color = source_color(getattr(item, field, ''))
        # A card with no metadata must not inherit another activity's colors.
        parts.append(f'{variable}:{color or "initial"}')
    return ';'.join(parts)


def _group_style(group, item):
    """Prefer the first source-colored activity in the spreadsheet's row order."""
    background = source_color(getattr(item, 'source_background', ''))
    rank = source_order(item)
    if background and (group.get('_color_rank') is None or rank < group['_color_rank']):
        group['style'] = activity_style(item)
        group['_color_rank'] = rank


def source_order(item):
    match=re.search(r'\d+',getattr(item,'source_cell','') or '')
    return int(match.group()) if match else 100000

def sector_rows(days):
    groups={}
    for index,day in enumerate(days):
        for kind in ['shifts','shared','incomplete']:
            for item in day[kind]:
                key=sector_key(item.sector)
                if key not in groups:
                    groups[key]={'name':item.sector,'hue':sector_hue(item.sector),'style':activity_style(item),'_color_rank':None,'order':source_order(item),'cells':[{'date':d['date'],'shifts':[],'shared':[],'incomplete':[],'count':0} for d in days]}
                group=groups[key];group['order']=min(group['order'],source_order(item))
                _group_style(group,item)
                group['cells'][index][kind].append(item)
                group['cells'][index]['count']+=1
    return sorted(groups.values(),key=lambda group:(group['order'],sector_key(group['name'])))

def month_sector_groups(shifts):
    """Chronological daily entries; group only consecutive matching sectors."""
    groups=[]
    ordered=sorted(shifts,key=lambda item:(item.starts_at,item.ends_at,sector_key(item.sector),item.pk or 0))
    for item in ordered:
        key=sector_key(item.sector)
        if not groups or sector_key(groups[-1]['name'])!=key:
            groups.append({'name':item.sector,'hue':sector_hue(item.sector),'style':activity_style(item),'_color_rank':None,'order':source_order(item),'shifts':[]})
        group=groups[-1]
        group['order']=min(group['order'],source_order(item))
        _group_style(group,item)
        group['shifts'].append(item)
    return groups
