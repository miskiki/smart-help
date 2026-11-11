"""Isolated atomic publication cache; source policy tables are never written."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from .settings import DATA

_local=threading.local()

@contextmanager
def connection():
    DATA.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(str(DATA/'publications.sqlite'),timeout=20)
    conn.row_factory=sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE IF NOT EXISTS publication (digest TEXT PRIMARY KEY,status TEXT NOT NULL,source TEXT NOT NULL,spec TEXT,fields TEXT,audit TEXT,attempts INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS catalog (singleton INTEGER PRIMARY KEY CHECK(singleton=1),digest TEXT NOT NULL,data TEXT NOT NULL,updated REAL NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS active_source (digest TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS supplement (base_digest TEXT PRIMARY KEY,body TEXT NOT NULL,updated REAL NOT NULL)')
    try:
        with conn:yield conn
    finally:conn.close()

def enqueue(sources):
    with connection() as c:
        c.execute('DELETE FROM active_source')
        c.executemany('INSERT INTO active_source VALUES(?)',[(key,) for key in sources])
        for digest,source in sources.items():
            c.execute('INSERT OR IGNORE INTO publication(digest,status,source,updated) VALUES(?,?,?,?)',(digest,'queued',json.dumps(source,ensure_ascii=False),time.time()))

def save(digest,status,spec=None,fields=None,audit=None):
    with connection() as c:
        prior=c.execute('SELECT attempts FROM publication WHERE digest=?',(digest,)).fetchone()
        if status=='retry' and prior and prior['attempts']>=2:status='quarantined'
        c.execute('UPDATE publication SET status=?,spec=?,fields=?,audit=?,attempts=attempts+1,updated=? WHERE digest=?',(status,json.dumps(spec,ensure_ascii=False),json.dumps(fields,ensure_ascii=False),json.dumps(audit,ensure_ascii=False),time.time(),digest))

def publications(digests):
    result={}
    with connection() as c:
        keys=list(set(digests))
        for i in range(0,len(keys),400):
            batch=keys[i:i+400]
            for r in c.execute('SELECT * FROM publication WHERE digest IN ('+','.join('?' for _ in batch)+')',batch):
                d=dict(r)
                for key in ('source','spec','fields','audit'):d[key]=json.loads(d[key] or 'null')
                result[r['digest']]=d
    return result

def queue(limit=4,priority=()):
    with connection() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE publication SET status='retry' WHERE status='processing' AND updated<?",(time.time()-600,))
        rows=c.execute("SELECT p.digest,p.source,p.status,p.attempts FROM publication p JOIN active_source a ON a.digest=p.digest WHERE p.status='queued' OR (p.status='retry' AND p.attempts<3 AND p.updated<?) ORDER BY p.updated LIMIT ?",(time.time()-60,10000)).fetchall()
        wanted=set(priority);rows=sorted(rows,key=lambda r:0 if r['digest'] in wanted else 1)[:limit]
        for r in rows:c.execute("UPDATE publication SET status='processing',updated=? WHERE digest=?",(time.time(),r['digest']))
    return [(r['digest'],json.loads(r['source'])) for r in rows[:limit]]

def save_catalog(digest,data):
    with connection() as c:c.execute('INSERT OR REPLACE INTO catalog VALUES(1,?,?,?)',(digest,json.dumps(data,ensure_ascii=False),time.time()))

def load_catalog():
    with connection() as c:r=c.execute('SELECT * FROM catalog WHERE singleton=1').fetchone()
    return dict(digest=r['digest'],data=json.loads(r['data']),updated=r['updated']) if r else None

def supplements():
    with connection() as c:return {r['base_digest']:r['body'] for r in c.execute('SELECT * FROM supplement')}

def save_supplement(key,body):
    with connection() as c:c.execute('INSERT OR REPLACE INTO supplement VALUES(?,?,?)',(key,body,time.time()))
