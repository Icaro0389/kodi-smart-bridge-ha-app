#!/usr/bin/env python3
"""Kodi Smart Bridge: localhost-only, no third-party dependencies."""
import json, os, re, socket, time, secrets, threading, ipaddress, sqlite3, codecs
from pathlib import Path
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get('BRIDGE_HOST', '127.0.0.1')
PORT = int(os.environ.get('BRIDGE_PORT', '8787'))
KODI_PORT = int(os.environ.get('KODI_TCP_PORT', '9090'))
KODI_HOST = os.environ.get('KODI_HOST', '127.0.0.1')
DATA_DIR = os.environ.get('BRIDGE_DATA_DIR', '/data/catalogs')
SETTINGS_PATH = os.environ.get('IPTVXC_SETTINGS_PATH', '/data/iptvxc-settings.xml')
TOKEN = os.environ.get('BRIDGE_TOKEN', '')
RADIO = os.environ.get('RADIO_M3U_PATH', '/share/kodi-smart-bridge/radio.m3u')
TV = os.environ.get('TV_M3U_PATH', '/share/kodi-smart-bridge/tv.m3u8')
ROOT = 'plugin://plugin.video.iptvxc/'
TTL = 1800
lock = threading.RLock()
handles = {} # opaque-id -> (expiry, underlying Kodi path, filetype)
cache = {} # path -> (expiry, raw directory listing)
search_indexes = {}  # root -> background index status, entries, progress
INDEX_TTL = 3600
MAX_INDEX_ITEMS = 60000

class BridgeError(Exception): pass

def rpc(method, params=None):
    req = {'jsonrpc':'2.0', 'id':secrets.token_hex(8), 'method':method, 'params':params or {}}
    decoder = json.JSONDecoder()
    try:
        with socket.create_connection((KODI_HOST, KODI_PORT), timeout=12) as s:
            s.settimeout(20)
            s.sendall(json.dumps(req, ensure_ascii=False).encode('utf-8'))
            data = ''
            while len(data) < 8_000_000:
                chunk = s.recv(65536)
                if not chunk: break
                data += chunk.decode('utf-8')
                # TCP notifications may precede the reply, without delimiters.
                while data.strip():
                    data = data.lstrip()
                    try: obj, used = decoder.raw_decode(data)
                    except json.JSONDecodeError: break
                    data = data[used:]
                    if obj.get('id') == req['id']:
                        if 'error' in obj: raise BridgeError('Kodi JSON-RPC error: ' + str(obj['error'].get('message', 'unknown')))
                        return obj.get('result', {})
            raise BridgeError('Kodi did not return a complete response')
    except (OSError, UnicodeError) as e:
        raise BridgeError('Kodi TCP not reachable / incomplete response') from e

def handle(path, typ):
    key = secrets.token_urlsafe(20)
    with lock:
        handles[key] = (time.monotonic() + TTL, path, typ)
        if len(handles) > 10000:
            now = time.monotonic()
            for k, v in list(handles.items()):
                if v[0] < now: handles.pop(k, None)
    return key

def resolve(key, typ=None):
    with lock: record = handles.get(key)
    if not record or record[0] < time.monotonic(): raise BridgeError('Invalid or expired item ID; reload the category')
    if typ and record[2] != typ: raise BridgeError('Wrong item type')
    return record[1]

def strip_markup(label):
    label = re.sub(r'\[/?(?:COLOR(?:\s+[^]]*)?|B|I|LIGHT|UPPERCASE)\]', '', label or '', flags=re.I)
    return label[:250]

def picture(value):
    # Only return a known public TMDb artwork URL, not Kodi local paths / credentials.
    if not isinstance(value, str): return None
    if value.startswith('image://'):
        from urllib.parse import unquote
        value = unquote(value[8:].rstrip('/'))
    p = urlparse(value)
    return value if p.scheme == 'https' and p.hostname == 'image.tmdb.org' else None

def radio_logo(value):
    if not isinstance(value, str): return None
    try:
        p = urlparse(value)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
            return None
        if p.port not in (None, 443): return None
        host = p.hostname.lower()
        if host == 'localhost' or host.endswith(('.local','.internal')): return None
        try:
            if not ipaddress.ip_address(host).is_global: return None
        except ValueError:
            pass
        return value
    except ValueError:
        return None

def get_listing(path):
    if not path.startswith(ROOT): raise BridgeError('Unsupported directory')
    with lock: entry = cache.get(path)
    if entry and entry[0] > time.monotonic(): return entry[1]
    raw = rpc('Files.GetDirectory', {'directory':path,'media':'files','properties':['title','thumbnail','fanart','plot','year','rating']})
    with lock:
        if len(cache) > 90: cache.clear()
        cache[path] = (time.monotonic() + 120, raw)
    return raw

def browse(path, page, size):
    raw = get_listing(path)
    files = raw.get('files') or []
    if not isinstance(files, list): files = []
    total = len(files)
    subset = files[page*size:(page+1)*size]
    out = []
    for f in subset:
        uri, typ = f.get('file', ''), f.get('filetype','')
        if not isinstance(uri, str) or not uri.startswith(ROOT) or typ not in ('file','directory'): continue
        label = strip_markup(f.get('label') or f.get('title'))
        out.append({'id':handle(uri,typ), 'type':typ, 'title':label,
                    'poster':picture(f.get('thumbnail')), 'plot':str(f.get('plot') or '')[:1000]})
    return {'items':out,'total':total,'page':page,'page_size':size,'has_more':(page+1)*size < total}


# Kodi IPTVXC exposes category pagination as navigable directory entries
# (e.g. "Pagina successiva >>"), not as a Kodi JSON-RPC limits field.
# Index all pages explicitly, while avoiding arbitrary subfolders and loops.
MAX_CATEGORY_PAGES = 150

def is_next_page(item):
    if item.get('filetype') != 'directory':
        return False
    label = strip_markup(item.get('label') or item.get('title')).casefold().strip()
    return bool(re.search(r'(?:pagina\s+successiva|next\s+page|pagina\s+seguente|page\s+next)', label))

def build_search_index(root_name):
    state = search_indexes[root_name]
    entry_path = ROOT + ('?url=smartvod&mode=90' if root_name == 'film' else '?url=live&mode=18')
    try:
        categories = get_listing(entry_path).get('files') or []
        dirs = [(f.get('file'), strip_markup(f.get('label') or f.get('title')))
                for f in categories if f.get('filetype') == 'directory'
                and isinstance(f.get('file'), str) and f['file'].startswith(ROOT)]
        with lock: state['total_categories'] = len(dirs)
        seen_items = set()
        for folder, category in dirs:
            pending = [folder]
            visited_pages = set()
            category_failed = False
            try:
                while pending and len(visited_pages) < MAX_CATEGORY_PAGES:
                    page_path = pending.pop(0)
                    if page_path in visited_pages: continue
                    visited_pages.add(page_path)
                    try:
                        raw = get_listing(page_path).get('files') or []
                        if not isinstance(raw, list):
                            raise BridgeError('Invalid Kodi directory listing')
                    except Exception:
                        category_failed = True
                        continue
                    with lock: state['pages_scanned'] += 1
                    for item in raw:
                        uri, kind = item.get('file'), item.get('filetype')
                        if not isinstance(uri, str) or not uri.startswith(ROOT) or kind not in ('file','directory'):
                            continue
                        if is_next_page(item):
                            if uri not in visited_pages and uri not in pending:
                                pending.append(uri)
                            continue
                        if uri in seen_items: continue
                        seen_items.add(uri)
                        record = {'uri':uri, 'kind':kind,
                                  'title':strip_markup(item.get('label') or item.get('title')),
                                  'poster':picture(item.get('thumbnail')), 'category':category}
                        with lock:
                            if len(state['entries']) >= MAX_INDEX_ITEMS:
                                state['truncated'] = True
                                break
                            state['entries'].append(record)
                    if state['truncated']: break
                if pending:
                    category_failed = True
                if state['truncated']: break
            except Exception:
                category_failed = True
            finally:
                with lock:
                    state['scanned_categories'] += 1
                    if category_failed: state['errors'] += 1
    except Exception:
        with lock: state['errors'] += 1
    finally:
        with lock:
            state['indexing'] = False
            state['completed_at'] = time.monotonic()

def search_catalog(root_name, term, page, size):
    if not isinstance(term, str) or not (2 <= len(term.strip()) <= 100):
        raise BridgeError('Search needs at least 2 characters')
    term = term.strip().casefold()
    with lock:
        state = search_indexes.get(root_name)
        if state is None or (not state['indexing'] and time.monotonic()-state['completed_at'] > INDEX_TTL):
            state = {'entries':[], 'indexing':True, 'scanned_categories':0,
                     'total_categories':0, 'pages_scanned':0, 'errors':0, 'truncated':False, 'completed_at':0}
            search_indexes[root_name] = state
            threading.Thread(target=build_search_index, args=(root_name,), daemon=True).start()
        # Snapshot under lock; Kodi directory calls happen in background.
        candidates = [e for e in state['entries'] if term in e['title'].casefold()]
        progress = {k:state[k] for k in ('indexing','scanned_categories','total_categories','pages_scanned','errors','truncated')}
    total = len(candidates)
    result=[]
    for e in candidates[page*size:(page+1)*size]:
        result.append({'id':handle(e['uri'],e['kind']), 'type':e['kind'],
                       'title':e['title'], 'poster':e['poster'], 'group':e['category']})
    return {'items':result, 'total':total, 'page':page, 'page_size':size,
            'has_more':(page+1)*size<total, **progress}





def rpc_big_directory(path, start, end):
    req = {'jsonrpc':'2.0','id':secrets.token_hex(12),'method':'Files.GetDirectory',
           'params':{'directory':path,'media':'files',
                     'properties':['title','thumbnail'],
                     'limits':{'start':start,'end':end}}}
    decoder=json.JSONDecoder()
    utf8=codecs.getincrementaldecoder('utf-8')()
    data=''
    try:
        with socket.create_connection((KODI_HOST,KODI_PORT),timeout=12) as sock:
            sock.settimeout(120)
            sock.sendall(json.dumps(req,ensure_ascii=False).encode('utf-8'))
            received=0
            while received < 12_000_000:
                chunk=sock.recv(65536)
                if not chunk: break
                received += len(chunk)
                data += utf8.decode(chunk)
                while data.strip():
                    data=data.lstrip()
                    try: obj,used=decoder.raw_decode(data)
                    except json.JSONDecodeError: break
                    data=data[used:]
                    if obj.get('id') != req['id']: continue
                    if 'error' in obj:
                        raise BridgeError('VOD Kodi JSON-RPC error')
                    return obj.get('result',{})
            raise BridgeError('VOD paginated reply incomplete or too large')
    except (OSError,UnicodeError) as exc:
        raise BridgeError('VOD Kodi timeout / connection error') from exc





# Preserve the SmartVOD index and combine only matching titles. This keeps
# the existing WebSocket/card contract and playback opaque handles unchanged.
_old_search_catalog = search_catalog


def series_search_sql(term, page, size):
    """Search IPTVXC series, opening seasons through its plugin."""
    import sqlite3
    import unicodedata
    import xml.etree.ElementTree as ET
    from pathlib import Path
    from urllib.parse import urlencode, urlsplit

    if not isinstance(term, str) or not 2 <= len(term.strip()) <= 100:
        raise BridgeError('Search needs at least 2 characters')

    page, size = int(page), int(size)
    if page < 0 or not 1 <= size <= 200:
        raise BridgeError('Invalid pagination')

    def normalize(value):
        s = unicodedata.normalize('NFKD', value)
        return ''.join(
            c for c in s if not unicodedata.combining(c)
        ).casefold()

    needle = normalize(term.strip())

    db = sqlite3.connect(
        f'file:{DATA_DIR}/series-json-index.sqlite3?mode=ro',
        uri=True,
        timeout=5
    )

    try:
        total = db.execute(
            'SELECT COUNT(*) FROM series '
            'WHERE instr(title_search, ?) > 0',
            (needle,)
        ).fetchone()[0]

        rows = db.execute(
            'SELECT series_id,title,poster,plot '
            'FROM series WHERE instr(title_search, ?) > 0 '
            'ORDER BY title_search, id LIMIT ? OFFSET ?',
            (needle, size, page * size)
        ).fetchall()
    finally:
        db.close()

    # Read the current Kodi addon settings at request time.
    # Credentials never enter the SQLite search index.
    settings_file = Path(SETTINGS_PATH)

    try:
        settings_root = ET.parse(settings_file).getroot()
        settings = {}
        for element in settings_root.iter('setting'):
            key = element.attrib.get('id')
            if key:
                settings[key] = (
                    element.attrib.get('value')
                    if 'value' in element.attrib
                    else (element.text or '')
                )
        dns = (settings.get('DNS') or '').strip().rstrip('/')
        username = settings.get('Username') or ''
        password = settings.get('Password') or ''

        parsed = urlsplit(dns)
        if (parsed.scheme not in ('http', 'https')
                or not parsed.netloc or not username or not password):
            raise ValueError('Incomplete addon settings')
    except (OSError, ET.ParseError, ValueError):
        raise BridgeError(
            'IPTVXC settings unavailable; cannot open series'
        )

    api = dns + '/player_api.php'

    items = []
    for sid, title, poster, plot in rows:
        endpoint = api + '?' + urlencode({
            'username': username,
            'password': password,
            'action': 'get_series_info',
            'series_id': sid
        })
        uri = ROOT + '?' + urlencode({
            'url': endpoint,
            'mode': '19',
            'name': title,
            'iconimage': poster or ''
        })
        items.append({
            'id': handle(uri, 'directory'),
            'type': 'directory',
            'title': title,
            'poster': picture(poster),
            'plot': plot or '',
            'group': 'Series'
        })

    return {
        'items': items,
        'total': total,
        'page': page,
        'page_size': size,
        'has_more': (page + 1) * size < total,
        'indexing': False,
        'scanned_categories': 0,
        'total_categories': 0,
        'pages_scanned': 0,
        'errors': 0,
        'truncated': False
    }


def search_catalog(root_name, term, page, size):
    if root_name == 'series':
        return series_search_sql(term, page, size)
    # Movies/VOD: search the complete IPTVXC XML-derived SQLite index.
    # Other roots keep their existing implementation.
    if root_name != 'film':
        return _old_search_catalog(root_name, term, page, size)

    import sqlite3
    import unicodedata

    if not isinstance(term, str) or not 2 <= len(term.strip()) <= 100:
        raise BridgeError('Search needs at least 2 characters')

    page = int(page)
    size = int(size)

    if page < 0 or not 1 <= size <= 200:
        raise BridgeError('Invalid pagination')

    def normalize(value):
        value = unicodedata.normalize('NFKD', value)
        return ''.join(
            ch for ch in value
            if not unicodedata.combining(ch)
        ).casefold()

    needle = normalize(term.strip())

    database = (
        f'{DATA_DIR}/vod-xml-index.sqlite3'
    )

    db = sqlite3.connect(
        'file:' + database + '?mode=ro',
        uri=True,
        timeout=5
    )

    try:
        condition = """
            instr(title_search, ?) > 0
            AND (
                stream_url LIKE 'http://%'
                OR stream_url LIKE 'https://%'
            )
        """

        total = db.execute(
            'SELECT COUNT(*) FROM movies WHERE ' + condition,
            (needle,)
        ).fetchone()[0]

        rows = db.execute(
            'SELECT title, stream_url, poster FROM movies WHERE '
            + condition +
            ' ORDER BY title_search, id LIMIT ? OFFSET ?',
            (needle, size, page * size)
        ).fetchall()

        catalog_total = db.execute(
            'SELECT COUNT(*) FROM movies'
        ).fetchone()[0]

    finally:
        db.close()

    items = []

    for title, stream_url, poster in rows:
        # Apply the same VOD port workaround used by IPTVXC.
        vod_host = os.environ.get('VOD_FIX_HOST', '').strip()
        vod_port = os.environ.get('VOD_FIX_PORT', '24000').strip()
        if vod_host and vod_port.isdigit() and 1 <= int(vod_port) <= 65535:
            stream_url = stream_url.replace(
                '://' + vod_host + '/',
                '://' + vod_host + ':' + vod_port + '/',
                1
            )

        items.append({
            'id': handle(stream_url, 'file'),
            'type': 'file',
            'title': title,
            'poster': picture(poster),
            'group': 'Movies/VOD'
        })

    return {
        'items': items,
        'total': total,
        'page': page,
        'page_size': size,
        'has_more': (page + 1) * size < total,
        'indexing': False,
        'scanned_categories': 0,
        'total_categories': 0,
        'pages_scanned': 0,
        'errors': 0,
        'truncated': False,
        'vod_indexing': False,
        'vod_scanned': catalog_total,
        'vod_total': catalog_total,
        'vod_complete': True,
        'vod_error': '',
        'vod_last_updated': 0
    }

def m3u_parse(filename, item_type):
    """Read trusted local M3U only. Keep URLs server-side, expose opaque handles."""
    path = Path(filename).resolve()
    if not path.is_file() or path.suffix.lower() not in ('.m3u', '.m3u8'):
        raise BridgeError('Playlist unavailable')
    if path.stat().st_size > 5_000_000:
        raise BridgeError('Playlist too large')
    text = path.read_text(encoding='utf-8-sig', errors='replace').splitlines()
    group, title, logo = 'Senza gruppo', None, None
    entries = []
    for line in text:
        line = line.strip()
        if line.startswith('#EXTINF:'):
            title = strip_markup(line.split(',', 1)[-1].strip()) if ',' in line else None
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line.split(',',1)[0]))
            group = (attrs.get('group-title') or 'Senza gruppo').strip()[:100]
            logo = attrs.get('tvg-logo', '')
        elif line.startswith('#EXTGRP:'):
            group = line[8:].strip()[:100] or 'Senza gruppo'
        elif line and not line.startswith('#'):
            parsed = urlparse(line)
            if parsed.scheme in ('http','https') and parsed.hostname:
                entries.append({'id':handle(line,item_type), 'type':'file',
                                'title':title or f'{item_type.upper()} {len(entries)+1}',
                                'group':group, 'poster':radio_logo(logo) if item_type == 'radio' else picture(logo)})
            title, logo, group = None, None, 'Senza gruppo'
            if len(entries) >= 3000: break
    return entries

def playlist_browse(kind, page, size, group=None, search=None):
    filename = TV if kind == 'tv' else RADIO
    entries = m3u_parse(filename, kind)
    groups = {}
    for entry in entries:
        groups[entry['group']] = groups.get(entry['group'], 0) + 1
    if group is not None:
        entries = [e for e in entries if e['group'] == group]
    if search:
        entries = [e for e in entries if search.casefold() in e['title'].casefold()]
    total = len(entries)
    return {'items':entries[page*size:(page+1)*size], 'total':total,
            'page':page,'page_size':size,'has_more':(page+1)*size < total,
            'groups':[{'title':k,'count':v} for k,v in groups.items()]}

def radio_plugin_uri(stream_url):
    """Resolve the station by its current ordered M3U index, without placing secrets in the plugin URL."""
    playlist = Path(RADIO)
    if not playlist.is_file():
        raise BridgeError('Radio playlist unavailable')
    idx = 0
    for line in playlist.read_text(encoding='utf-8-sig', errors='replace').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parsed = urlparse(line)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            continue
        if line == stream_url:
            return 'plugin://plugin.audio.kodismart/?station=' + str(idx)
        idx += 1
    raise BridgeError('Radio station no longer in playlist; reload radio')

def native_favourites():
    """Read Kodi's actual favourites, never exposing raw IPTV paths to clients."""
    result = rpc('Favourites.GetFavourites', {
        'properties': ['path', 'window', 'windowparameter', 'thumbnail']
    })
    return result.get('favourites') or []


def favourite_target(entry):
    kind = entry.get('type')
    if kind == 'media':
        return entry.get('path') or ''
    if kind == 'window' and (entry.get('window') or '').lower() in ('videos', 'video'):
        return entry.get('windowparameter') or ''
    return ''


def favourite_items():
    items = []
    for favourite in native_favourites():
        uri = favourite_target(favourite)
        # Only expose actionable paths from the IPTVXC plugin. Native
        # favourites from other addons are retained in Kodi, not modified.
        parsed = urlparse(uri)
        if not (uri.startswith(ROOT) or (
            favourite.get('type') == 'media'
            and parsed.scheme in ('http', 'https') and parsed.hostname)):
            continue
        directory = favourite.get('type') == 'window'
        items.append({
            'id': handle(uri, 'directory' if directory else 'file'),
            'type': 'directory' if directory else 'file',
            'title': strip_markup(favourite.get('title') or 'Senza titolo'),
            'poster': picture(favourite.get('thumbnail') or ''),
            'favourite': True,
            'group': 'Preferiti Kodi'
        })
    return {'items': items, 'total': len(items), 'page': 0,
            'page_size': max(1, len(items)), 'has_more': False}


def favourite_add(key, title, poster=None):
    if not isinstance(key, str) or len(key) > 128:
        raise BridgeError('Invalid ID')
    if not isinstance(title, str) or not 1 <= len(title) <= 250:
        raise BridgeError('Invalid title')
    with lock:
        record = handles.get(key)
    uri = resolve(key)
    if not record or record[2] not in ('file', 'directory'):
        raise BridgeError('Only movies and series can be favourited')
    if record[2] == 'directory' and not uri.startswith(ROOT):
        raise BridgeError('Unsupported series directory')
    if record[2] == 'file' and not (uri.startswith(ROOT) or
            urlparse(uri).scheme in ('http', 'https')):
        raise BridgeError('Unsupported media path')
    if any(favourite_target(fav) == uri for fav in native_favourites()):
        return {'ok': True, 'already_saved': True}
    options = {'title': strip_markup(title), 'thumbnail': picture(poster) or ''}
    if record[2] == 'directory':
        options.update({'type': 'window', 'window': 'videos',
                        'windowparameter': uri})
    else:
        options.update({'type': 'media', 'path': uri})
    rpc('Favourites.AddFavourite', options)
    return {'ok': True, 'already_saved': False}


def radio_items():
    if not RADIO: return {'configured':False, 'items':[]}
    result = playlist_browse('radio', 0, 2000)
    return {'configured':True, **result}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Avoid logging user-supplied values and sensitive stream URLs.
        pass
    def reply(self, status, data):
        raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers(); self.wfile.write(raw)
    def auth(self):
        return bool(TOKEN) and secrets.compare_digest(self.headers.get('Authorization',''), 'Bearer '+TOKEN)
    def process(self, post=False):
        if not self.auth(): return self.reply(401, {'error':'Unauthorized'})
        try:
            route = self.path.split('?',1)[0]
            data = {}
            if post:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 8192: return self.reply(413, {'error':'Invalid request size'})
                data = json.loads(self.rfile.read(length))
                if not isinstance(data,dict): raise BridgeError('Object expected')
            if route == '/v1/health' and not post:
                result = rpc('JSONRPC.Version')
                return self.reply(200, {'ok':True,'kodi':result})
            if route == '/v1/favourites' and not post:
                return self.reply(200, favourite_items())
            if route == '/v1/favourite' and post:
                return self.reply(200, favourite_add(
                    data.get('id'), data.get('title'), data.get('poster')))
            if route == '/v1/roots' and not post:
                return self.reply(200, {'roots':[
                    {'id':'film','title':'SmartVOD'}, {'id':'series','title':'Serie TV'},
                    {'id':'tv','title':'Live TV (M3U)'}, {'id':'radio','title':'Radio (M3U)'}, {'id':'movies','title':'Movies/VOD'}]})
            if route == '/v1/browse' and post:
                root = {'film':'?url=smartvod&mode=90', 'series':'?url=live&mode=18',
                        'movies':'?url=vod&mode=3'}
                if data.get('root') in ('tv','radio') and not data.get('id'):
                    kind = data['root']
                    page, size = data.get('page',0), data.get('page_size',24)
                    if type(page) is not int or not 0 <= page <= 10000: raise BridgeError('Invalid page')
                    if type(size) is not int or not 1 <= size <= 100: raise BridgeError('Invalid page size')
                    group, search = data.get('group'), data.get('search')
                    if group is not None and (not isinstance(group,str) or len(group)>100): raise BridgeError('Invalid group')
                    if search is not None and (not isinstance(search,str) or len(search)>100): raise BridgeError('Invalid search')
                    return self.reply(200, playlist_browse(kind,page,size,group,search))
                if data.get('root') in ('film','series') and not data.get('id') and data.get('search'):
                    page, size = data.get('page',0), data.get('page_size',24)
                    if type(page) is not int or not 0 <= page <= 10000: raise BridgeError('Invalid page')
                    if type(size) is not int or not 1 <= size <= 100: raise BridgeError('Invalid page size')
                    return self.reply(200, search_catalog(data['root'], data['search'], page, size))
                if data.get('root') in root and not data.get('id'):
                    path = ROOT + root[data['root']]
                elif isinstance(data.get('id'),str): path = resolve(data['id'],'directory')
                else: raise BridgeError('Provide a valid root or directory ID')
                page, size = data.get('page',0), data.get('page_size',24)
                if type(page) is not int or page < 0 or page > 10000: raise BridgeError('Invalid page')
                if type(size) is not int or not 1 <= size <= 100: raise BridgeError('Invalid page size')
                return self.reply(200,browse(path,page,size))
            if route == '/v1/play' and post:
                key = data.get('id')
                if not isinstance(key,str): raise BridgeError('Invalid ID')
                with lock:
                    record = handles.get(key)
                uri = resolve(key)
                # Only radio items are routed via the metadata-aware Kodi addon.
                if record and record[2] == 'radio':
                    uri = radio_plugin_uri(uri)
                result = rpc('Player.Open', {'item': {'file': uri}})
                return self.reply(200, {'ok':True,'result':result})
            if route == '/v1/radio' and not post: return self.reply(200,radio_items())
            return self.reply(404, {'error':'Not found'})
        except (BridgeError, ValueError, TypeError, json.JSONDecodeError) as e:
            return self.reply(400, {'error':str(e)[:180]})
        except Exception:
            return self.reply(500, {'error':'Internal error'})
    def do_GET(self): self.process(False)
    def do_POST(self): self.process(True)

if __name__ == '__main__':
    if len(TOKEN) < 32: raise SystemExit('BRIDGE_TOKEN must be set to a random 32+ character secret')
    print(f'Kodi Smart Bridge listening on {HOST}:{PORT}',flush=True)
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
