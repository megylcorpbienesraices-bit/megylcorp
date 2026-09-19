"""v1.27.18 · Sesiones revocables y cadena de suministro hash-complete."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from app.core import net_guard

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _session_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("ITM_SESSION_STATE_PATH", str(tmp_path / "session_state.json"))
    monkeypatch.setenv("ITM_ACCESS_TOKEN", "t" * 48)
    prev_epoch, prev_revoked = net_guard._REVOKE_EPOCH, dict(net_guard._REVOKED)
    net_guard._REVOKE_EPOCH = 0
    net_guard._REVOKED.clear()
    yield
    net_guard._REVOKE_EPOCH = prev_epoch
    net_guard._REVOKED.clear()
    net_guard._REVOKED.update(prev_revoked)


def test_sessions_have_unique_128bit_sid():
    issued = {net_guard.issue_session(now=1_000_000.0) for _ in range(50)}
    assert len(issued) == 50
    sid = net_guard.session_sid(next(iter(issued)))
    assert re.fullmatch(r"[0-9a-f]{32}", sid or "")


def test_session_credential_never_contains_master_token(monkeypatch):
    token = "S3CR3T" + "z" * 42
    monkeypatch.setenv("ITM_ACCESS_TOKEN", token)
    cred = net_guard.issue_session()
    assert token not in cred and "S3CR3T" not in cred


def test_revoke_one_session_does_not_revoke_another():
    a, b = net_guard.issue_session(), net_guard.issue_session()
    assert net_guard.session_valid(a) and net_guard.session_valid(b)
    assert net_guard.revoke_session(a) is True
    assert not net_guard.session_valid(a)
    assert net_guard.session_valid(b)


def test_revocation_survives_restart_simulation():
    a, b = net_guard.issue_session(), net_guard.issue_session()
    assert net_guard.revoke_session(a)
    net_guard._REVOKED.clear()
    net_guard._load_session_state()
    assert not net_guard.session_valid(a)
    assert net_guard.session_valid(b)


def test_expired_or_malformed_credentials_do_not_pollute_revocation_list():
    old = net_guard.issue_session(now=1_000.0)
    assert net_guard.revoke_session(old) is False
    for value in (None, "", "junk", "v1.99999999999.deadbeef", "v2.x.y.z"):
        assert net_guard.revoke_session(value) is False
    assert not net_guard._REVOKED


def test_revoke_all_invalidates_every_old_session_without_rotating_master_token():
    token_before = os.environ["ITM_ACCESS_TOKEN"]
    old = [net_guard.issue_session() for _ in range(5)]
    assert all(net_guard.session_valid(x) for x in old)
    net_guard.revoke_all_sessions()
    assert not any(net_guard.session_valid(x) for x in old)
    assert os.environ["ITM_ACCESS_TOKEN"] == token_before
    assert net_guard.session_valid(net_guard.issue_session())


def test_revoke_all_persists_across_restart_simulation():
    old = net_guard.issue_session()
    net_guard.revoke_all_sessions()
    net_guard._REVOKE_EPOCH = 0
    net_guard._load_session_state()
    assert not net_guard.session_valid(old)


def test_v1_credentials_and_tampered_v2_credentials_are_rejected():
    assert not net_guard.session_valid("v1.99999999999.deadbeef")
    legacy_payload = "v1.99999999999"
    assert not net_guard.session_valid(f"{legacy_payload}.{net_guard._sign(legacy_payload)}")
    cred = net_guard.issue_session()
    v, exp, sid, mac = cred.split(".")
    assert not net_guard.session_valid(f"{v}.{exp}.{sid}.{'0' * len(mac)}")
    assert not net_guard.session_valid(f"{v}.{int(exp)+99999}.{sid}.{mac}")
    assert not net_guard.session_valid(f"{v}.{exp}.{'f'*32}.{mac}")


def test_corrupt_session_state_fails_closed_and_state_file_is_private():
    cred = net_guard.issue_session()
    assert net_guard.revoke_session(cred)
    path = Path(net_guard._session_state_path())
    assert path.exists()
    assert (path.stat().st_mode & 0o077) == 0
    another = net_guard.issue_session()
    path.write_text("{broken json", encoding="utf-8")
    net_guard._REVOKE_EPOCH = 0
    net_guard._REVOKED.clear()
    net_guard._load_session_state()
    assert not net_guard.session_valid(another)


def test_logout_revokes_server_side_and_logout_all_is_protected():
    from fastapi.testclient import TestClient
    from app.main import app

    assert "/logout" not in net_guard.OPEN_PATHS
    assert "/logout-all" not in net_guard.OPEN_PATHS
    assert "/health" not in net_guard.OPEN_PATHS
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert 'net_guard.revoke_session(' in source
    assert '@app.post("/logout-all")' in source

    cred = net_guard.issue_session()
    c = TestClient(app)
    c.cookies.set(net_guard.COOKIE_NAME, cred)
    response = c.post("/logout")
    assert response.status_code == 200
    assert response.json().get("revoked") is True
    assert not net_guard.session_valid(cred)


LOCKS = (
    "requirements.production.lock.txt",
    "requirements.test.lock.txt",
    "requirements.rust-bridge.lock.txt",
)


@pytest.mark.parametrize("name", LOCKS)
def test_every_pinned_package_has_at_least_one_sha256(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    starts = list(re.finditer(r"(?m)^([a-z0-9][a-z0-9._-]*)==", text))
    assert starts, f"{name} pins no packages"
    missing = []
    for i, match in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        block = text[match.start():end]
        if "--hash=sha256:" not in block:
            missing.append(match.group(1))
    assert not missing, f"{name}: packages without hashes: {missing}"


def test_production_lock_contains_transitive_closure_sentinels():
    text = (ROOT / "requirements.production.lock.txt").read_text(encoding="utf-8")
    packages = {p.lower() for p in re.findall(r"(?m)^([a-z0-9][a-z0-9._-]*)==", text)}
    for dep in ("pydantic", "anyio", "certifi", "urllib3", "h11", "idna", "markupsafe", "packaging", "typing-extensions"):
        assert dep in packages, f"missing transitive dependency {dep}"


def test_docker_and_ci_require_hash_verification_and_no_deps():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes" in docker and "--no-deps" in docker
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    installs = re.findall(r"pip install[^\n]*requirements[^\n]*", ci)
    assert installs
    assert all("--require-hashes" in line and "--no-deps" in line for line in installs)


def test_each_lock_has_reproducible_input_file():
    for name in LOCKS:
        inp = ROOT / name.replace(".lock.txt", ".in")
        assert inp.exists(), f"missing input file for {name}: {inp.name}"
