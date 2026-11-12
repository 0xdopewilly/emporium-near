"""Emporium against the official TypeScript x402 ``exact``-on-NEAR (@x402/near), offline.

interop/run.mjs --offline runs both implementations' clients and facilitators against tests/fake_near.py's
in-memory chain: each side's payloads verified by the other, the tamper matrix on the same bytes at both,
and a settle each way. The Borsh fuzz compares near.py with near-api-js on random delegates.

Both need Node and ``cd interop && npm ci``; without them these tests are skipped, so the suite stays green
on a machine with Python only. The testnet run (real chain, real settles) is interop/README.md's.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INTEROP = ROOT / "interop"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    not NODE or not (INTEROP / "node_modules" / "@x402" / "near").is_dir()
    or not (INTEROP / "node_modules" / "near-api-js").is_dir(),
    reason="needs node and interop/node_modules (cd interop && npm ci)")

# What testnet USDC refuses to pay (interop/README.md has the transactions): Emporium refuses them at verify.
CHAIN_REFUSES = ("E01", "E02", "E03", "E04", "E06", "E07", "E09")


def test_official_and_emporium_verify_each_others_payloads_and_agree_on_every_tamper(tmp_path):
    out = tmp_path / "interop.json"
    run = subprocess.run([NODE, "run.mjs", "--offline", "--n", "12", "--m", "12", "--settle", "--out", str(out)],
                         cwd=INTEROP, env={**os.environ, "PY": sys.executable}, capture_output=True, text=True,
                         timeout=300)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    r = json.loads(out.read_text())
    assert r["versions"]["@x402/near"] == "2.30.0"
    for d in ("direction1", "direction2"):
        assert r[d]["errors"] == 0
        assert r[d]["acceptedByOfficial"] == r[d]["acceptedByEmporium"] == 12, r[d]["rejections"]
    s = r["tamper"]["summary"]
    assert s["tampered"] >= 60 and s["bothReject"] == s["tampered"]
    assert s["controls"] >= 5 and s["bothAccept"] == s["controls"]
    assert s["agree"] == s["judged"] and not s["errors"]
    assert not s["emporiumNamesPayerOfficialDoesNot"], "a payer is named only once its signature verifies"
    edges = {e["id"]: e for e in s["edges"]}
    assert all(edges[k]["emporium"] is False for k in CHAIN_REFUSES), edges
    assert len(r["settles"]) == 2 and all(x["confirmed"] for x in r["settles"])
    assert all(not x["crossReplaySettle"]["success"] for x in r["settles"]), "a settled payment never pays twice"


def test_borsh_fuzz_matches_near_api_js():
    run = subprocess.run([sys.executable, "scripts/check_borsh.py", "interop", "--fuzz", "300", "--seed", "11"],
                         cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]
    assert "7 of 7 identical" in run.stdout and "300 of 300 identical" in run.stdout
