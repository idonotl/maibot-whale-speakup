"""Persistent local attempt ledger; no chat text and no secrets."""
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
import sqlite3


class Ledger:
    def __init__(self, path: Path):
        self.path=path
        self.lock=RLock()

    @contextmanager
    def connect(self):
        connection=sqlite3.connect(self.path, timeout=5)
        connection.row_factory=sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def start(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
              CREATE TABLE IF NOT EXISTS target_state (
                target TEXT PRIMARY KEY, last_attempt REAL NOT NULL DEFAULT 0,
                last_queued REAL NOT NULL DEFAULT 0, last_outcome TEXT NOT NULL DEFAULT '',
                day TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
                misses INTEGER NOT NULL DEFAULT 0, stream_id TEXT NOT NULL DEFAULT '');
              CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, target TEXT NOT NULL,
                outcome TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
            ''')

    def get(self, target):
        with self.connect() as c:
            row=c.execute('SELECT * FROM target_state WHERE target=?',(target,)).fetchone()
            return dict(row) if row else dict(target=target,last_attempt=0,last_queued=0,last_outcome='',day='',attempts=0,misses=0,stream_id='')

    def log(self, at, target, outcome, detail=''):
        with self.lock,self.connect() as c:
            c.execute('INSERT INTO decisions(at,target,outcome,detail) VALUES(?,?,?,?)',(at,target,outcome,detail[:200]))
            c.execute('DELETE FROM decisions WHERE id NOT IN (SELECT id FROM decisions ORDER BY id DESC LIMIT 500)')

    def miss(self, at, target, detail):
        with self.lock,self.connect() as c:
            c.execute('INSERT INTO target_state(target,misses) VALUES(?,1) ON CONFLICT(target) DO UPDATE SET misses=MIN(misses+1,100)',(target,))
            c.execute('INSERT INTO decisions(at,target,outcome,detail) VALUES(?,?,?,?)',(at,target,'miss',detail))

    def dry_hit(self, at, target, detail):
        with self.lock,self.connect() as c:
            c.execute('INSERT INTO target_state(target,misses) VALUES(?,0) ON CONFLICT(target) DO UPDATE SET misses=0',(target,))
            c.execute('INSERT INTO decisions(at,target,outcome,detail) VALUES(?,?,?,?)',(at,target,'dry_hit',detail))

    def reserve(self, at, day, target, stream_id, daily_max, cooldown_seconds, last_human):
        """Reserve before RPC: survives restart and prevents duplicate attempts."""
        with self.lock,self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT OR IGNORE INTO target_state(target) VALUES(?)',(target,))
            row=dict(c.execute('SELECT * FROM target_state WHERE target=?',(target,)).fetchone())
            count=row['attempts'] if row['day']==day else 0
            if count>=daily_max or (row['last_attempt'] and at-row['last_attempt']<cooldown_seconds):
                return False
            if row['last_queued'] and row['last_outcome'] in ('reserved','queued','uncertain') and last_human<=row['last_queued']:
                return False
            c.execute('UPDATE target_state SET last_attempt=?,last_queued=?,last_outcome=?,day=?,attempts=?,misses=0,stream_id=? WHERE target=?',
                      (at,at,'reserved',day,count+1,stream_id,target))
            c.execute('INSERT INTO decisions(at,target,outcome,detail) VALUES(?,?,?,?)',(at,target,'reserved','quota reserved before task'))
            return True

    def finish(self, at, target, outcome, detail=''):
        with self.lock,self.connect() as c:
            c.execute('UPDATE target_state SET last_outcome=? WHERE target=?',(outcome,target))
            c.execute('INSERT INTO decisions(at,target,outcome,detail) VALUES(?,?,?,?)',(at,target,outcome,detail[:200]))

    def recent(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT at,target,outcome,detail FROM decisions ORDER BY id DESC LIMIT 30')]
