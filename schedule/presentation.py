"""Stable sector colors and spreadsheet-style grouping, independent of ownership."""
import hashlib
import re
import unicodedata
from functools import lru_cache

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
                    groups[key]={'name':item.sector,'hue':sector_hue(item.sector),'order':source_order(item),'cells':[{'date':d['date'],'shifts':[],'shared':[],'incomplete':[],'count':0} for d in days]}
                group=groups[key];group['order']=min(group['order'],source_order(item))
                group['cells'][index][kind].append(item)
                group['cells'][index]['count']+=1
    return sorted(groups.values(),key=lambda group:(group['order'],sector_key(group['name'])))

def month_sector_groups(shifts):
    grouped={}
    for item in shifts:
        key=sector_key(item.sector)
        if key not in grouped:grouped[key]={'name':item.sector,'hue':sector_hue(item.sector),'order':source_order(item),'shifts':[]}
        grouped[key]['order']=min(grouped[key]['order'],source_order(item))
        grouped[key]['shifts'].append(item)
    return sorted(grouped.values(),key=lambda group:(group['order'],sector_key(group['name'])))
