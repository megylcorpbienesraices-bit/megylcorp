"""v1.27.21 · endpoint symbol scope must never depend on an undefined local."""
from __future__ import annotations
import ast
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MAIN=ROOT/'app'/'main.py'

def _fn(name:str):
    tree=ast.parse(MAIN.read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name==name:
            return node
    raise AssertionError(name)

def _loaded_names(fn):
    return {n.id for n in ast.walk(fn) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load)}

def _assigned_names(fn):
    out=set(a.arg for a in fn.args.args)
    for n in ast.walk(fn):
        if isinstance(n,ast.Name) and isinstance(n.ctx,(ast.Store,ast.Param)): out.add(n.id)
    return out

def test_api_state_defines_hsym_before_use():
    fn=_fn('api_state')
    assert '_hsym' in _loaded_names(fn)
    assert '_hsym' in _assigned_names(fn)

def test_live_scheduler_defines_hsym_before_use():
    fn=_fn('api_nextgen_live_scheduler')
    assert '_hsym' in _loaded_names(fn)
    assert '_hsym' in _assigned_names(fn)

def test_no_other_endpoint_loads_hsym_without_assignment():
    tree=ast.parse(MAIN.read_text(encoding='utf-8'))
    bad=[]
    for fn in [n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]:
        if '_hsym' in _loaded_names(fn) and '_hsym' not in _assigned_names(fn): bad.append(fn.name)
    assert not bad, bad
