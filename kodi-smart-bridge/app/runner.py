#!/usr/bin/env python3
"""Pilot supervisor: run catalog refresh then service; refresh every configured interval."""
import json, os, pathlib, subprocess, sys, threading, time
opts=json.loads(pathlib.Path('/data/options.json').read_text())
def refresh():
    while True:
        completed=subprocess.run([sys.executable,'/app/refresh-direct-catalogs.py'],timeout=300,check=False)
        print('Catalog refresh status:', 'ok' if completed.returncode==0 else 'failed',flush=True)
        time.sleep(int(opts['refresh_hours'])*3600)
threading.Thread(target=refresh,daemon=True).start()
proc=subprocess.Popen([sys.executable,'/app/bridge.py'])
sys.exit(proc.wait())
