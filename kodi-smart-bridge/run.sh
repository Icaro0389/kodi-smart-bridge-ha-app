#!/bin/sh
set -eu
python3 - <<'PYINNER'
import json, os, pathlib, sys, xml.etree.ElementTree as ET
opts=json.loads(pathlib.Path('/data/options.json').read_text())
required=('bridge_token','kodi_host','iptv_dns','iptv_username','iptv_password')
if not all(str(opts.get(k,'')).strip() for k in required):
    sys.exit('Configuration incomplete: enter bridge token, Kodi host and IPTV credentials')
if len(opts['bridge_token']) < 32: sys.exit('bridge_token must contain at least 32 characters')
from urllib.parse import urlsplit
parsed=urlsplit(opts['iptv_dns'])
if parsed.scheme not in ('http','https') or not parsed.netloc: sys.exit('iptv_dns is invalid')
root=ET.Element('settings')
for key,val in [('DNS',opts['iptv_dns']),('Username',opts['iptv_username']),('Password',opts['iptv_password'])]:
    ET.SubElement(root,'setting',{'id':key,'value':val})
p=pathlib.Path('/data/iptvxc-settings.xml')
ET.ElementTree(root).write(p, encoding='utf-8', xml_declaration=True)
p.chmod(0o600)
pathlib.Path('/data/catalogs').mkdir(mode=0o700,exist_ok=True)
PYINNER
export BRIDGE_HOST=0.0.0.0 BRIDGE_PORT=8787
export KODI_HOST="$(python3 -c 'import json;print(json.load(open("/data/options.json"))["kodi_host"])')"
export KODI_TCP_PORT="$(python3 -c 'import json;print(json.load(open("/data/options.json"))["kodi_port"])')"
export BRIDGE_TOKEN="$(python3 -c 'import json;print(json.load(open("/data/options.json"))["bridge_token"])')"
export TV_M3U_PATH="$(python3 -c 'import json;print(json.load(open("/data/options.json"))["tv_playlist"])')"
export RADIO_M3U_PATH="$(python3 -c 'import json;print(json.load(open("/data/options.json"))["radio_playlist"])')"
export VOD_FIX_HOST="$(python3 -c 'import json;print(json.load(open("/data/options.json")).get("vod_fix_host",""))')"
export VOD_FIX_PORT="$(python3 -c 'import json;print(json.load(open("/data/options.json")).get("vod_fix_port",24000))')"
export BRIDGE_DATA_DIR=/data/catalogs IPTVXC_SETTINGS_PATH=/data/iptvxc-settings.xml
python3 /app/runner.py
