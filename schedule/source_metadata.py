"""Read activity heading colors and right-hand notes without changing identity."""
import colorsys
import re
from xml.etree import ElementTree

from openpyxl.styles.colors import COLOR_INDEX

HEX=re.compile(r'^[0-9A-Fa-f]{6}$')


def _rgb(value):
    value=str(value or '')
    value=value[-6:] if len(value)==8 else value
    return '#'+value.upper() if HEX.fullmatch(value) else ''


def theme_palette(book):
    theme=getattr(book,'loaded_theme',None)
    if not theme:return {'lt1':'#FFFFFF','dk1':'#000000'}
    try:
        root=ElementTree.fromstring(theme)
        ns={'a':'http://schemas.openxmlformats.org/drawingml/2006/main'}
        scheme=root.find('.//a:clrScheme',ns)
        result={}
        if scheme is not None:
            for item in scheme:
                for color in item:
                    result[item.tag.rsplit('}',1)[-1]]=_rgb(color.attrib.get('lastClr') or color.attrib.get('val'))
        return result
    except (ElementTree.ParseError,TypeError):return {}


def color_hex(color,palette):
    if color is None:return ''
    value=''
    if color.type=='rgb':value=_rgb(color.rgb)
    elif color.type=='indexed' and isinstance(color.indexed,int) and 0<=color.indexed<len(COLOR_INDEX):value=_rgb(COLOR_INDEX[color.indexed])
    elif color.type=='theme':
        names=['lt1','dk1','lt2','dk2','accent1','accent2','accent3','accent4','accent5','accent6','hlink','folHlink']
        if isinstance(color.theme,int) and 0<=color.theme<len(names):value=palette.get(names[color.theme],'')
    if value and color.tint:
        r,g,b=[int(value[i:i+2],16)/255 for i in [1,3,5]]
        h,l,s=colorsys.rgb_to_hls(r,g,b)
        tint=max(-1,min(1,float(color.tint)))
        l=l*(1+tint) if tint<0 else l*(1-tint)+tint
        r,g,b=colorsys.hls_to_rgb(h,max(0,min(1,l)),s)
        value='#'+''.join(f'{round(channel*255):02X}' for channel in [r,g,b])
    return value


def heading_colors(cell,palette):
    fill=getattr(cell,'fill',None)
    background=color_hex(fill.fgColor,palette) if fill and fill.patternType=='solid' else '#FFFFFF'
    font=getattr(cell,'font',None)
    foreground=color_hex(font.color,palette) if font else ''
    return {'source_background':background,'source_foreground':foreground or '#000000'}


def right_note(row,column):
    # The weekly block is date, time, two reference columns, F/G names, notes.
    value=row[column+6] if len(row)>column+6 else None
    if value is None:return ''
    if isinstance(value,(str,int,float)) and not isinstance(value,bool):
        return str(value).strip()[:2000]
    return ''


def update_metadata(instance,metadata):
    changed=[]
    for field,value in metadata.items():
        if getattr(instance,field)!=value:
            setattr(instance,field,value);changed.append(field)
    if changed:instance.save(update_fields=changed)
