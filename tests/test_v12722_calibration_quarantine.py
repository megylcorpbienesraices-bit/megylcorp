"""v1.27.22 · malformed calibration rows are quarantined, never silently trusted."""
from __future__ import annotations

import hashlib
import json
import warnings
from pathlib import Path

import pandas as pd

from app.core import calibration as cal
from app.core import flow_intelligence as flow
from conftest import assert_version_at_least, assert_marker_version_at_least


def test_malformed_scanner_csv_quarantines_only_bad_rows(tmp_path: Path):
    src=tmp_path / "scanner_history_dia_2026-09-14.csv"
    src.write_text(
        "timestamp,direction,evidence_score\n"
        "2026-09-14T13:30:00Z,BUY,72\n"
        "2026-09-14T13:31:00Z,SELL,65,EXTRA_FIELD\n"
        "2026-09-14T13:32:00Z,SELL,61\n",
        encoding="utf-8",
    )
    before=hashlib.sha256(src.read_bytes()).hexdigest()
    df,meta=cal._read_scanner_history_csv(src,tmp_path,"DIA")
    assert len(df)==2
    assert list(df["evidence_score"].astype(str))==["72","61"]
    assert meta["recovered"] is True and meta["quarantined_rows"]==1
    q=Path(meta["quarantine_file"])
    assert q.is_file()
    row=json.loads(q.read_text(encoding="utf-8").splitlines()[0])
    assert row["kind"]=="MALFORMED_CALIBRATION_CSV_ROW"
    assert row["line"]==3 and row["expected_fields"]==3 and row["actual_fields"]==4
    assert meta["source_repaired"] is True
    forensic=Path(meta["forensic_source_copy"])
    assert forensic.is_file() and hashlib.sha256(forensic.read_bytes()).hexdigest()==before
    clean=pd.read_csv(src)
    assert len(clean)==2 and list(clean["evidence_score"])==[72,61]


def test_same_malformed_file_uses_stable_recovery_without_duplicate_quarantine(tmp_path: Path):
    src=tmp_path / "scanner_history_dia_2026-09-14.csv"
    src.write_text(
        "timestamp,direction,evidence_score\n"
        "2026-09-14T13:30:00Z,BUY,72\n"
        "2026-09-14T13:31:00Z,SELL,65,EXTRA_FIELD\n",
        encoding="utf-8",
    )
    a,ma=cal._read_scanner_history_csv(src,tmp_path,"DIA")
    b,mb=cal._read_scanner_history_csv(src,tmp_path,"DIA")
    assert len(a)==len(b)==1
    assert a["timestamp"].astype(str).tolist()==b["timestamp"].astype(str).tolist()
    assert a["direction"].astype(str).tolist()==b["direction"].astype(str).tolist()
    assert a["evidence_score"].astype(str).tolist()==b["evidence_score"].astype(str).tolist()
    assert ma["source_repaired"] is True
    assert mb["recovered"] is False, "el segundo arranque ya debe leer un CSV sano"
    q=Path(ma["quarantine_file"])
    assert len(q.read_text(encoding="utf-8").splitlines())==1


def test_flow_concat_preserves_union_schema_without_pandas_futurewarning(tmp_path: Path, monkeypatch):
    old=pd.DataFrame({
        "timestamp":[pd.Timestamp("2026-09-14T13:30:00")],
        "contract_symbol":["DIA260914C00500000"],
        "trade_price":[1.0],"contracts":[10],"old_only":[1.0],"all_na":[None],
    })
    new=pd.DataFrame({
        "timestamp":[pd.Timestamp("2026-09-14T13:31:00")],
        "contract_symbol":["DIA260914C00501000"],
        "trade_price":[1.1],"contracts":[12],"new_only":[2.0],"all_na":[None],
    })
    out=tmp_path/"flow.csv"
    monkeypatch.setattr(flow,"load_flow_events",lambda *a,**k:old.copy())
    monkeypatch.setattr(flow,"_flow_path",lambda *a,**k:out)
    monkeypatch.setattr(flow,"enrich_option_packages",lambda x:x)
    with warnings.catch_warnings():
        warnings.simplefilter("error",FutureWarning)
        flow.save_flow_events(new,"DIA")
    got=pd.read_csv(out)
    assert len(got)==2
    assert {"old_only","new_only","all_na"}.issubset(got.columns)


def test_release_identity_at_least_v12722():
    assert_version_at_least("1.27.22")
    assert_marker_version_at_least("1.27.22")
