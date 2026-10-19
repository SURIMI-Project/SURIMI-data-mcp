"""Tests for the /audit human-verification routes (page, verify, receipt fetch)."""
from __future__ import annotations

import base64
import copy
import json

import duckdb
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from surimi_data_mcp.engine import aggregate as agg_mod
from surimi_data_mcp.provenance import lineage
from surimi_data_mcp.provenance import receipt_store
from surimi_data_mcp.web.audit_routes import make_audit_routes
from surimi_data_mcp.engine.cube import Cube


def _canonical_bytes(receipt: dict) -> bytes:
    """Byte-identical to receipts._canonical_bytes / audit_link.py: the exact
    signed bytes the page carries in the fragment / store / paste."""
    return json.dumps(receipt, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _b64url_fragment(receipt: dict) -> str:
    """Return the canonical bytes in URL-safe base64 with '=' stripped, as audit_link.py writes them.

    The audit page's b64urlDecode adds the padding back before decoding."""
    return base64.urlsafe_b64encode(_canonical_bytes(receipt)).decode("ascii").rstrip("=")


@pytest.fixture(autouse=True)
def _reset():
    yield
    lineage.reset()
    receipt_store.reset()


@pytest.fixture()
def cube(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", str(tmp_path / "absent.json"))
    monkeypatch.delenv("SURIMI_CUBE_DB", raising=False)
    lineage.reset()
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ds1 (country VARCHAR, v INTEGER)")
    con.execute("INSERT INTO ds1 VALUES ('NO', 3)")
    return Cube(con)


@pytest.fixture()
def client(cube):
    return TestClient(Starlette(routes=make_audit_routes(
        cube_factory=lambda: cube, query_client_factory=None)))


def test_audit_page_served(client):
    r = client.get("/audit")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Recompute" in r.text


def test_audit_page_honest_failure_states(client):
    """Recompute failure and missing-secret states must be honest, not scary."""
    text = client.get("/audit").text
    assert "RECOMPUTE FAILED" in text
    assert "NOT evidence" in text
    assert "cannot verify signatures" in text


def test_audit_page_no_encodeuri_href(client):
    """Source links must go through the https? allowlist, not encodeURI."""
    text = client.get("/audit").text
    assert "encodeURI(" not in text
    assert "^https?:" in text


def test_audit_page_claim_mentions_limit(client):
    """A top-N claim must surface the row limit, not read as unqualified."""
    assert "limited to" in client.get("/audit").text


def test_audit_verify_valid_with_recompute(client, cube):
    receipt = agg_mod.aggregate(cube, "ds1", [{"col": "v", "fn": "sum"}],
                                dimensions=["country"])["receipt"]
    r = client.post("/audit/verify", json={"receipt": receipt, "recompute": True})
    body = r.json()
    assert body["verified"] is True and body["match"] is True


def test_audit_verify_tampered(client, cube):
    receipt = agg_mod.aggregate(cube, "ds1", [{"col": "v", "fn": "sum"}],
                                dimensions=["country"])["receipt"]
    receipt = {**receipt, "output_value": {"rows": [{"country": "NO", "v_sum": 666}]}}
    body = client.post("/audit/verify",
                       json={"receipt": receipt, "recompute": True}).json()
    assert body["verified"] is False and "match" not in body


def test_audit_verify_rejects_non_dict(client):
    assert client.post("/audit/verify", json={"receipt": "x"}).status_code == 400


def test_audit_verify_rejects_invalid_json(client):
    r = client.post("/audit/verify", content=b"not-json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_receipt_fetch_roundtrip(client, cube):
    receipt = agg_mod.aggregate(cube, "ds1", [{"col": "v", "fn": "sum"}])["receipt"]
    raw = json.dumps(receipt, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    sig = receipt["signature"]
    assert receipt_store.put_raw(sig, raw) == sig
    r = client.get(f"/audit/receipt/{sig}")
    assert r.status_code == 200
    assert r.content == raw                       # verbatim bytes
    assert "application/json" in r.headers["content-type"]


def test_receipt_fetch_miss_is_helpful_404(client):
    r = client.get("/audit/receipt/" + "ab" * 32)
    assert r.status_code == 404
    assert "paste" in r.json().get("hint", "").lower()


def test_audit_verify_rejects_oversized_body(client):
    """Bodies over 1MB are rejected with 413 before being parsed."""
    big = b'{"receipt": {"pad": "' + b"x" * 1_100_000 + b'"}}'
    r = client.post("/audit/verify", content=big,
                    headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert "error" in r.json()


def test_audit_verify_accepts_normal_sized_body(client, cube):
    """The body cap must not reject ordinary receipts."""
    receipt = agg_mod.aggregate(cube, "ds1", [{"col": "v", "fn": "sum"}])["receipt"]
    r = client.post("/audit/verify", json={"receipt": receipt})
    assert r.status_code == 200


def test_recompute_limiter_serializes_to_one():
    """Anonymous recomputes share one DuckDB connection -- they must be
    serialized through a single-token CapacityLimiter (also blunts replay-DoS)."""
    import anyio

    from surimi_data_mcp.web import audit_routes
    limiter = audit_routes._RECOMPUTE_LIMITER
    assert isinstance(limiter, anyio.CapacityLimiter)
    assert limiter.total_tokens == 1


# --- raw-bytes verify paths (b64 / sig / text): the float-sum bug fix --------


@pytest.fixture()
def float_cube(tmp_path, monkeypatch):
    """A cube whose SUM over a DOUBLE column yields a whole-numbered FLOAT
    (1320.0) -- the exact value JS drops the '.0' from on JSON.stringify."""
    monkeypatch.setenv("SURIMI_RECEIPT_SECRET", "test-secret")
    monkeypatch.setenv("SURIMI_SOURCES_MANIFEST", str(tmp_path / "absent.json"))
    monkeypatch.delenv("SURIMI_CUBE_DB", raising=False)
    lineage.reset()
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE ds1 (country VARCHAR, loa DOUBLE)")
    con.execute("INSERT INTO ds1 VALUES ('BEL', 660.0), ('BEL', 660.0)")
    return Cube(con)


@pytest.fixture()
def float_client(float_cube):
    return TestClient(Starlette(routes=make_audit_routes(
        cube_factory=lambda: float_cube, query_client_factory=None)))


def _float_sum_receipt(cube):
    receipt = agg_mod.aggregate(cube, "ds1", [{"col": "loa", "fn": "sum"}],
                                dimensions=["country"])["receipt"]
    # Guard: this fixture only proves the bug if the signed value is a FLOAT
    # whose decimal part is .0 (the JS-drops-'.0' trigger).
    v = receipt["output_value"]["rows"][0]["loa_sum"]
    assert isinstance(v, float) and v == 1320.0
    return receipt


def test_verify_via_receipt_b64_float_sum_stays_valid(float_client, float_cube):
    """A genuine FLOAT-valued SUM receipt verifies via the b64 fragment path
    (the float-stable path). The legacy {receipt: <JS-mutated-to-int>} path is
    what was wrongly reporting it INVALID -- shown here to prove the b64 path
    is what saves it."""
    receipt = _float_sum_receipt(float_cube)

    # b64 fragment path: the page sends the raw signed bytes; the server
    # decodes them Python-side (json.loads("1320.0") -> 1320.0), so the
    # signature still covers exactly these bytes.
    frag = _b64url_fragment(receipt)
    body = float_client.post("/audit/verify",
                             json={"receipt_b64": frag, "recompute": False}).json()
    assert body["verified"] is True

    # Demonstrate the bug the b64 path fixes: mimic the browser dropping '.0'
    # by mutating the float to int, then POST as the legacy {receipt: dict}.
    # JS would have re-serialized 1320.0 as 1320 -> different signed bytes ->
    # signature mismatch -> "SIGNATURE INVALID" on a GENUINE receipt.
    js_mutated = copy.deepcopy(receipt)
    row = js_mutated["output_value"]["rows"][0]
    assert row["loa_sum"] == 1320.0
    row["loa_sum"] = int(row["loa_sum"])  # 1320.0 -> 1320, as JSON.stringify does
    legacy = float_client.post("/audit/verify",
                               json={"receipt": js_mutated, "recompute": False}).json()
    assert legacy["verified"] is False


def test_verify_via_receipt_b64_recompute_match(float_client, float_cube):
    """The b64 path also supports recompute: a genuine float receipt MATCHes."""
    receipt = _float_sum_receipt(float_cube)
    frag = _b64url_fragment(receipt)
    body = float_client.post("/audit/verify",
                             json={"receipt_b64": frag, "recompute": True}).json()
    assert body["verified"] is True and body["match"] is True


def test_verify_via_receipt_b64_tampered(float_client, float_cube):
    """A tampered b64 fragment is reported INVALID, never recomputed."""
    receipt = _float_sum_receipt(float_cube)
    tampered = copy.deepcopy(receipt)
    tampered["output_value"]["rows"][0]["loa_sum"] = 999999.0  # signed field
    frag = _b64url_fragment(tampered)
    body = float_client.post("/audit/verify",
                             json={"receipt_b64": frag, "recompute": True}).json()
    assert body["verified"] is False and "match" not in body


def test_verify_via_receipt_sig(float_client, float_cube):
    """receipt_sig path: server reads the verbatim stored bytes by signature."""
    receipt = _float_sum_receipt(float_cube)
    raw = _canonical_bytes(receipt)
    sig = receipt["signature"]
    assert receipt_store.put_raw(sig, raw) == sig
    body = float_client.post("/audit/verify",
                             json={"receipt_sig": sig, "recompute": False}).json()
    assert body["verified"] is True


def test_verify_via_receipt_sig_unknown_is_404_with_paste_hint(float_client):
    """An unknown sig is a helpful 404 telling the user to paste the JSON."""
    r = float_client.post("/audit/verify",
                          json={"receipt_sig": "ab" * 32, "recompute": False})
    assert r.status_code == 404
    j = r.json()
    assert j.get("error") and "paste" in j.get("hint", "").lower()


def test_verify_via_receipt_text(float_client, float_cube):
    """receipt_text path: server parses the raw canonical JSON string (paste)."""
    receipt = _float_sum_receipt(float_cube)
    raw_text = _canonical_bytes(receipt).decode("utf-8")
    body = float_client.post("/audit/verify",
                             json={"receipt_text": raw_text, "recompute": False}).json()
    assert body["verified"] is True


def test_verify_via_receipt_text_invalid_json_is_400(float_client):
    """A non-JSON receipt_text is a clear 400, not a 500."""
    r = float_client.post("/audit/verify",
                          json={"receipt_text": "not-json", "recompute": False})
    assert r.status_code == 400
    assert "error" in r.json()


def test_verify_via_receipt_b64_invalid_is_400(float_client):
    """A b64 string that decodes to non-JSON is a clear 400."""
    bad = base64.urlsafe_b64encode(b"not-json").decode("ascii").rstrip("=")
    r = float_client.post("/audit/verify",
                          json={"receipt_b64": bad, "recompute": False})
    assert r.status_code == 400
    assert "error" in r.json()


def test_verify_deeply_nested_json_bomb_is_400_not_500(float_client):
    """A deeply-nested JSON array (well under the body cap) raises RecursionError
    in json.loads; on this PUBLIC endpoint it must be a clean 400, never a 500."""
    bomb = "[" * 20000 + "]" * 20000          # ~40KB, depth ~20000
    assert float_client.post(
        "/audit/verify", json={"receipt_text": bomb}).status_code == 400
    b64 = base64.urlsafe_b64encode(bomb.encode()).decode("ascii").rstrip("=")
    assert float_client.post(
        "/audit/verify", json={"receipt_b64": b64}).status_code == 400


def test_verify_legacy_receipt_dict_still_works(client, cube):
    """The legacy {receipt: dict} path (open-webui actions POST it server-side,
    unaffected by JS float drift) must keep verifying genuine receipts."""
    receipt = agg_mod.aggregate(cube, "ds1", [{"col": "v", "fn": "sum"}],
                                dimensions=["country"])["receipt"]
    body = client.post("/audit/verify",
                       json={"receipt": receipt, "recompute": True}).json()
    assert body["verified"] is True and body["match"] is True


# --- 2026-10-14: the page leads with what was calculated, in words and SQL; the signature is one line ---

def test_audit_page_leads_with_the_explanation_not_the_signature(client):
    text = client.get("/audit").text
    assert "<title>How this number was calculated</title>" in text
    assert "signed receipt" not in text.lower()
    order = [text.index(f'card("{t}")') for t in
             ("What was calculated", "About this table", "The query", "Source", "Re-run it")]
    assert order == sorted(order)
    assert 'card("Integrity")' not in text


def test_dataset_notes_endpoint(client):
    r = client.get("/audit/dataset/eu-dcf-catches")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "eu-dcf-catches" and "LOWER BOUND" in body["note"]
    assert client.get("/audit/dataset/no-such-thing").status_code == 404


_SHIM = r"""
function E(tag){ return {tag, children:[], className:"", _t:"", listeners:{}, href:undefined,
  set textContent(v){ this._t=String(v); this.children=[]; }, get textContent(){ return this._t + this.children.map(c=>c.textContent||"").join(""); },
  appendChild(c){ this.children.push(c); return c; }, prepend(c){ this.children.unshift(c); return c; },
  replaceChildren(...c){ this.children=c; this._t=""; }, addEventListener(k,f){ this.listeners[k]=f; },
  replaceChild(n, o){ const i = this.children.indexOf(o); if (i < 0) throw new Error("not a child"); this.children[i] = n; return o; } }; }
const nodes = {out: E("main"), integrity: E("footer")};
global.document = { createElement: E, createTextNode: t => ({textContent: String(t)}), getElementById: id => nodes[id] };
global.window = { addEventListener(){} }; global.location = { hash: "" };
global.navigator = { clipboard: { writeText: () => Promise.resolve() } };
const VERIFIED = process.argv[3] === "ok";
global.fetch = (url, opts) => Promise.resolve({ ok: true, json: () => Promise.resolve(
  String(url).startsWith("/audit/dataset/") ? {id: "eu-dcf-catches", name: "EU DCF catches", kind: "observed",
      note: "EVERY TOTAL HERE IS A LOWER BOUND."}
  : (VERIFIED ? {verified: true, reason: "ok"} : {verified: false, reason: "signature_mismatch"})) });
global.atob = s => Buffer.from(s, "base64").toString("binary");
global.TextDecoder = require("util").TextDecoder;
"""
_DUMP = r"""
function flat(n, acc){ if (!n) return acc; const t = n._t !== undefined ? n._t : n.textContent;
  if (n.tag) acc.push({tag: n.tag, cls: n.className, text: t || "", href: n.href});
  (n.children||[]).forEach(c => flat(c, acc)); return acc; }
const r = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
renderReceipt(r, {receipt_text: "x"});
setTimeout(() => console.log(JSON.stringify({out: flat(nodes.out, []), footer: flat(nodes.integrity, [])})), 20);
"""


def _render(receipt: dict, verified: bool, tmp_path) -> dict:
    import json as _json
    import re as _re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node is not installed")
    html = (Path(__file__).resolve().parents[1] / "static" / "audit.html").read_text(encoding="utf-8")
    script = _re.search(r"<script>(.*)</script>", html, _re.S).group(1).replace("\nboot();", "\n")
    js = tmp_path / "render.js"
    js.write_text(_SHIM + script + _DUMP, encoding="utf-8")
    rf = tmp_path / "r.json"
    rf.write_text(_json.dumps(receipt), encoding="utf-8")
    out = subprocess.run([node, str(js), str(rf), "ok" if verified else "bad"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return _json.loads(out.stdout)


_RECEIPT = {
    "version": 1, "tool_id": "aggregate", "timestamp": "2026-10-14T07:00:00Z", "signature": "a" * 64,
    "input_params": {"dataset": "hive_eu_dcf_eu_dcf_catches", "measures": [{"col": "landings_t", "fn": "sum"}]},
    "output_value": {"rows": [{"year": 2020, "landings_t_sum": 10.5}]},
    "provenance": {"human_check": {
        "sql": "SELECT SUM(landings_t) FROM t", "engine": "duckdb-cube",
        "source": {"file": "EU_DCF_Catches.csv", "url": "https://x.example/sources/EU_DCF_Catches.csv",
                   "agency": "JRC", "n_rows": 9},
        "explanation": {"summary": "Sum of `landings_t` in “EU DCF catches”.", "datasets": ["eu-dcf-catches"]}}},
}


def test_the_page_renders_a_receipt_in_the_order_a_reader_needs(tmp_path):
    got = _render(_RECEIPT, True, tmp_path)
    texts = [n["text"] for n in got["out"] if n["tag"] in ("h2", "p", "pre", "td")]
    heads = [n["text"] for n in got["out"] if n["tag"] == "h2"]
    assert heads == ["What was calculated", "About this table", "The query", "Source", "Re-run it"]
    assert "Sum of `landings_t` in “EU DCF catches”." in texts
    assert any("LOWER BOUND" in t for t in texts)          # the catalogue note, fetched by id
    assert "SELECT SUM(landings_t) FROM t" in texts
    assert any(n["tag"] == "a" and n["href"] == "https://x.example/sources/EU_DCF_Catches.csv" for n in got["out"])
    assert any("Unchanged since the server produced it" in n["text"] for n in got["footer"] if n["tag"] == "p")
    assert not any("Limited to" in t for t in texts)


def test_a_record_that_fails_the_check_is_flagged_on_top_and_gets_no_link(tmp_path):
    got = _render(_RECEIPT, False, tmp_path)
    first = got["out"][1]                                  # [0] is the page's main element
    assert first["cls"] == "card banner" and "was changed after the server produced it" in first["text"]
    assert not any(n["tag"] == "a" for n in got["out"])     # a forged record gets no clickable source


def test_an_older_record_says_it_predates_the_explanations(tmp_path):
    old = {**_RECEIPT, "provenance": {"human_check": {k: v for k, v in _RECEIPT["provenance"]["human_check"].items()
                                                      if k != "explanation"}}}
    got = _render(old, True, tmp_path)
    texts = [n["text"] for n in got["out"]]
    assert any("made before the server explained its numbers" in t for t in texts)


def test_audit_page_script_parses():
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node is not installed")
    html = (Path(__file__).resolve().parents[1] / "static" / "audit.html").read_text(encoding="utf-8")
    script = re.search(r"<script>(.*)</script>", html, re.S).group(1)
    out = subprocess.run([node, "--check", "-"], input=script, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
