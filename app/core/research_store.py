"""Local institutional research store for v1.14 (SQLite standard library)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


class ResearchStore:
    def __init__(self,path:Path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self._init()
    def _conn(self):
        c=sqlite3.connect(self.path,timeout=5);c.execute("PRAGMA journal_mode=WAL");return c
    def _init(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS quant_cycles(
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, symbol TEXT, mode TEXT, expiry_mode TEXT,
                spot REAL, gamma_center REAL, delta_center REAL, gamma_flip REAL, regime TEXT,
                scanner_direction TEXT, edge_state TEXT, evidence REAL, data_quality REAL, model_health REAL,
                dealer_gex REAL, hedge_pressure REAL, payload_json TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cycles_symbol_time ON quant_cycles(symbol,timestamp)")
            c.execute("""CREATE TABLE IF NOT EXISTS tape_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE, timestamp TEXT, symbol TEXT, expiry_mode TEXT,
                scanner_direction TEXT, edge_state TEXT, evidence REAL, zone_low REAL, zone_center REAL, zone_high REAL,
                target1 REAL, invalidation REAL, tape_state TEXT, zone_entry_time TEXT, progress_pct REAL,
                signed_volume REAL, volume REAL, buy_pct REAL, seconds_in_zone REAL, seconds_remaining REAL, trades INTEGER,
                price_move_favourable REAL, efficiency_per_1k REAL, imbalance_ratio REAL, aggression_score REAL,
                aggression_control TEXT, aggression_churn_pct REAL, aggression_absorption_pct REAL, cadence_regime TEXT,
                bars_per_minute REAL, payload_json TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tape_symbol_time ON tape_events(symbol,timestamp)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tape_symbol_state ON tape_events(symbol,tape_state)")
    def append_cycle(self, row:Dict[str,Any]):
        with self._conn() as c:
            c.execute("""INSERT INTO quant_cycles(timestamp,symbol,mode,expiry_mode,spot,gamma_center,delta_center,gamma_flip,regime,scanner_direction,edge_state,evidence,data_quality,model_health,dealer_gex,hedge_pressure,payload_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                str(row.get("timestamp",datetime.now(timezone.utc).isoformat())),str(row.get("symbol","")),str(row.get("mode","")),str(row.get("expiry_mode","")),row.get("spot"),row.get("gamma_center"),row.get("delta_center"),row.get("gamma_flip"),str(row.get("regime","")),str(row.get("scanner_direction","")),str(row.get("edge_state","")),row.get("evidence"),row.get("data_quality"),row.get("model_health"),row.get("dealer_gex"),row.get("hedge_pressure"),json.dumps(row.get("payload",{}),ensure_ascii=False,default=str)))

    def append_tape_event(self, row:Dict[str,Any]):
        """Persist one Tape Confirmation state transition.

        event_key makes repeated UI/chart refreshes idempotent: the same state during
        the same uninterrupted visit to the Scanner zone is stored once.
        """
        event_key=str(row.get("event_key") or "").strip()
        if not event_key:
            return False
        with self._conn() as c:
            cur=c.execute("""INSERT OR IGNORE INTO tape_events(
                event_key,timestamp,symbol,expiry_mode,scanner_direction,edge_state,evidence,zone_low,zone_center,zone_high,
                target1,invalidation,tape_state,zone_entry_time,progress_pct,signed_volume,volume,buy_pct,seconds_in_zone,
                seconds_remaining,trades,price_move_favourable,efficiency_per_1k,imbalance_ratio,aggression_score,
                aggression_control,aggression_churn_pct,aggression_absorption_pct,cadence_regime,bars_per_minute,payload_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                event_key,str(row.get("timestamp",datetime.now(timezone.utc).isoformat())),str(row.get("symbol","")),
                str(row.get("expiry_mode","")),str(row.get("scanner_direction","")),str(row.get("edge_state","")),
                row.get("evidence"),row.get("zone_low"),row.get("zone_center"),row.get("zone_high"),row.get("target1"),
                row.get("invalidation"),str(row.get("tape_state","")),str(row.get("zone_entry_time","") or ""),
                row.get("progress_pct"),row.get("signed_volume"),row.get("volume"),row.get("buy_pct"),
                row.get("seconds_in_zone"),row.get("seconds_remaining"),row.get("trades"),row.get("price_move_favourable"),
                row.get("efficiency_per_1k"),row.get("imbalance_ratio"),row.get("aggression_score"),
                str(row.get("aggression_control","") or ""),row.get("aggression_churn_pct"),row.get("aggression_absorption_pct"),
                str(row.get("cadence_regime","") or ""),row.get("bars_per_minute"),
                json.dumps(row.get("payload",{}),ensure_ascii=False,default=str)))
            return bool(cur.rowcount)

    def status(self,symbol:str)->Dict[str,Any]:
        with self._conn() as c:
            n=c.execute("SELECT COUNT(*) FROM quant_cycles WHERE symbol=?",(str(symbol).upper(),)).fetchone()[0]
            days=c.execute("SELECT COUNT(DISTINCT substr(timestamp,1,10)) FROM quant_cycles WHERE symbol=?",(str(symbol).upper(),)).fetchone()[0]
            first=c.execute("SELECT MIN(timestamp),MAX(timestamp) FROM quant_cycles WHERE symbol=?",(str(symbol).upper(),)).fetchone()
            tape_n=c.execute("SELECT COUNT(*) FROM tape_events WHERE symbol=?",(str(symbol).upper(),)).fetchone()[0]
            tape_terminal=c.execute("SELECT COUNT(*) FROM tape_events WHERE symbol=? AND tape_state IN ('CONFIRMED','ABSORBED','REJECTED','CHURN','EXPIRED')",(str(symbol).upper(),)).fetchone()[0]
        return {"backend":"SQLITE/WAL","path":str(self.path),"cycles":int(n),"sessions":int(days),"first":first[0],"last":first[1],
                "tape_events":int(tape_n),"tape_terminal_events":int(tape_terminal),
                "research_ready":bool(days>=20 and n>=500),
                "note":"Research store persistente; Tape se guarda como timing/confirmación separado y nunca reescribe la dirección del Scanner."}
