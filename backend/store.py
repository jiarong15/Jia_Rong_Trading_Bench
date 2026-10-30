"""SQLite transaction boundary with a hash-linked audit journal."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .engine import INITIAL_CASH, INITIAL_POSITIONS, DEFAULT_LIMITS

def now(): return datetime.now(timezone.utc).isoformat()
def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)

class Store:
    def __init__(self,path):
        self.path=str(path);Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS workspace (id INTEGER PRIMARY KEY, document TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, kind TEXT, payload TEXT, prev_hash TEXT, hash TEXT)')
            costs={'AAPL':231.80,'MSFT':412.32,'NVDA':126.45,'UST5':97.90,'UST10':95.65}
            state={'cash':INITIAL_CASH,'positions':INITIAL_POSITIONS,'costs':costs,'realized_pnl':0,'halted':False,'limits':DEFAULT_LIMITS,'runs':[],'orders':[],'backtests':[],'watchlist':['AAPL','UST10','EURUSD','XAU']}
            if not db.execute('SELECT id FROM workspace WHERE id=1').fetchone():
                db.execute('INSERT INTO workspace VALUES(1,?)',(canonical(state),))
                self.event(db,'workspace.created',{'mode':'synthetic-paper','opening_cash':INITIAL_CASH,'opening_positions':INITIAL_POSITIONS})
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row;return db
    @contextmanager
    def transaction(self):
        db=self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            state=json.loads(db.execute('SELECT document FROM workspace WHERE id=1').fetchone()[0])
            yield db,state
            db.execute('UPDATE workspace SET document=? WHERE id=1',(canonical(state),));db.commit()
        except Exception:
            db.rollback();raise
        finally: db.close()
    def read(self):
        with self.connect() as db: return json.loads(db.execute('SELECT document FROM workspace WHERE id=1').fetchone()[0])
    def event(self,db,kind,payload):
        previous=db.execute('SELECT hash FROM events ORDER BY seq DESC LIMIT 1').fetchone()
        prev=previous[0] if previous else ''; stamp=now(); body=canonical(payload)
        digest=hashlib.sha256(canonical([stamp,kind,body,prev]).encode()).hexdigest()
        db.execute('INSERT INTO events(time,kind,payload,prev_hash,hash) VALUES(?,?,?,?,?)',(stamp,kind,body,prev,digest))
    def audit(self):
        with self.connect() as db: rows=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY seq')]
        prev=''
        for r in rows:
            digest=hashlib.sha256(canonical([r['time'],r['kind'],r['payload'],prev]).encode()).hexdigest()
            if r['prev_hash']!=prev or r['hash']!=digest: raise ValueError('Ledger integrity verification failed.')
            prev=r['hash'];r['payload']=json.loads(r['payload'])
        return rows
    def reconcile(self,state,provider):
        """Rebuild the cash/quantity book from opening balances and fill events."""
        events=self.audit()
        opening=events[0]['payload'];cash=opening['opening_cash'];positions=dict(opening['opening_positions'])
        for record in events:
            if record['kind']!='order.filled': continue
            fill=record['payload'];i=provider.instrument(fill['instrument_id'])
            sign=1 if fill['side']=='buy' else -1
            positions[i['id']]=positions.get(i['id'],0)+sign*fill['quantity']
            cash=round(cash-sign*fill['quantity']*fill['fill_price']*i['multiplier']-fill['fee'],6)
        expected={k:v for k,v in positions.items() if v}
        actual={k:v for k,v in state['positions'].items() if v}
        if expected!=actual or abs(cash-state['cash'])>.00001:
            raise ValueError('Portfolio book does not reconcile to the fill journal. Execution is blocked.')
