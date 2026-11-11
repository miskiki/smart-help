"""Source-only cache. No family facts or credentials are stored here."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import threading

_locks={};_guard=threading.Lock()

def source_lock(key):
    with _guard:return _locks.setdefault(key,threading.Lock())

@contextmanager
def connection():
    root=Path(os.environ.get('FULL_MODEL_CACHE_DIR',str(Path(__file__).resolve().parents[1]/'data/full_model')))
    root.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(root/'interpretations.sqlite',timeout=15);conn.row_factory=sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS interpretation (key TEXT PRIMARY KEY,status TEXT NOT NULL,payload TEXT,error TEXT,updated REAL NOT NULL)')
    try:
        with conn:yield conn
    finally:conn.close()

def get(key):
    with connection() as conn:row=conn.execute('SELECT * FROM interpretation WHERE key=?',(key,)).fetchone()
    if not row:return None
    value=dict(row)
    if value['status']=='failed' and time.time()-value['updated']>120:return None
    value['payload']=json.loads(value['payload']) if value['payload'] else None
    return value

def save(key,status,payload=None,error=None):
    with connection() as conn:conn.execute('INSERT OR REPLACE INTO interpretation VALUES(?,?,?,?,?)',(key,status,json.dumps(payload,ensure_ascii=False,default=str) if payload else None,error,time.time()))
