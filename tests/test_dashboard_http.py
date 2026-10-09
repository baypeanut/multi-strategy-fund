"""Read-only dashboard boundary and browser injection regressions."""

import base64
import hashlib
import http.client
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading

import pytest

from dashboard.server import _HTML, make_handler, start_dashboard
from http.server import ThreadingHTTPServer


@pytest.fixture
def server(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"nav0": 10000, "systems": {}, "ticks": 7}))
    running = []

    def create(password=None, allowed_hosts=None, demo_mode=False):
        srv = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(path, password=password, allowed_hosts=allowed_hosts, demo_mode=demo_mode),
        )
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        running.append(srv)
        return srv, path

    yield create
    for srv in running:
        srv.shutdown()
        srv.server_close()


def request(srv, path="/", headers=None):
    con = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=3)
    con.request("GET", path, headers=headers or {})
    response = con.getresponse()
    body = response.read()
    result = (response.status, dict(response.getheaders()), body)
    con.close()
    return result


def test_state_routes_are_exact_read_only_and_have_privacy_headers(server):
    srv, _ = server()
    code, headers, body = request(srv, "/api/state?fresh=1")
    assert code == 200 and json.loads(body)["ticks"] == 7
    assert headers["Cache-Control"] == "no-store" and headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer" and headers["X-Frame-Options"] == "DENY"
    assert request(srv, "/api/state-extra")[0] == 404
    assert request(srv, "/.env")[0] == 404


def test_csp_digest_matches_served_inline_script(server):
    srv, _ = server()
    code, headers, body = request(srv)
    script = re.search(r"<script>(.*?)</script>", body.decode(), re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert "'sha256-" + digest + "'" in headers["Content-Security-Policy"]
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "'unsafe-eval'" not in headers["Content-Security-Policy"]


def test_demo_is_labeled_before_its_first_fetch(server):
    srv, _ = server(demo_mode=True)
    status, _, body = request(srv)
    assert status == 200
    assert b'id=demoBanner data-demo="true"' in body
    assert ">DEMO · SYNTHETIC</span>".encode() in body
    assert "SYNTHETIC · UI DEMO".encode() in body


def test_missing_or_invalid_state_reports_unavailable_instead_of_success(server):
    srv, path = server()
    path.unlink()
    code, _, body = request(srv, "/api/state")
    assert code == 503 and json.loads(body) == {"error": "state_unavailable"}
    path.write_text("{invalid")
    assert request(srv, "/api/state")[0] == 503


def test_remote_auth_challenge_and_secret_not_echoed(server):
    secret = "a-long-random-test-password"
    srv, _ = server(password=secret)
    code, headers, body = request(srv, "/api/state")
    assert code == 401 and "Basic realm=" in headers["WWW-Authenticate"]
    assert secret.encode() not in body
    token = base64.b64encode(("fund:" + secret).encode()).decode()
    assert request(srv, "/api/state", {"Authorization": "Basic " + token})[0] == 200
    assert request(srv, "/api/state", {"Authorization": "Basic not-base64"})[0] == 401


def test_loopback_host_restriction_blocks_dns_rebinding(server):
    srv, _ = server(allowed_hosts={"localhost", "127.0.0.1"})
    assert request(srv, "/api/state", {"Host": "attacker.example"})[0] == 403
    assert request(srv, "/api/state", {"Host": "localhost:8080"})[0] == 200


def test_public_bind_refuses_missing_auth_without_opening_socket(monkeypatch):
    monkeypatch.delenv("FUND_DASHBOARD_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="Remote dashboard requires"):
        start_dashboard(host="0.0.0.0", port=0)


def run_browser_regression(state, assertions):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for browser regressions; HTTP tests remain active")
    script = re.search(r"<script>\n(.*?)</script>", _HTML, re.S).group(1)
    source = (
        r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, {
    innerHTML: '', textContent: '', style: {}, dataset: {},
    classList: {add() {}, remove() {}, toggle() {}},
    getContext() {return {};}, querySelectorAll() {return [];}, appendChild() {}
  });
  return elements.get(id);
}
class Chart {
  constructor() {this.data = {}; this.options = {};}
  destroy() {} update() {}
}
const context = {
  STATE: __DATA__,
  document: {getElementById: el, querySelectorAll() {return [];}, createElement() {return el('created');}},
  Chart, Date, Map, Math, Number, String, Object, Array, JSON, AbortSignal,
  setInterval() {}, setTimeout() {}, fetch: async () => {throw Error('offline');}
};
vm.createContext(context);
vm.runInContext(__SCRIPT__, context);
(async () => {
  // Let the page's first fetch settle before testing later refreshes.
  await new Promise(resolve => setImmediate(resolve));
  __ASSERTIONS__
})().catch(error => {console.error(error); process.exitCode = 1;});
""".replace("__SCRIPT__", json.dumps(script))
        .replace("__DATA__", json.dumps(state))
        .replace("__ASSERTIONS__", assertions)
    )
    proc = subprocess.run([node, "-e", source], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr


def test_browser_escapes_untrusted_state_in_actual_render_function():
    attack = '<img src=x onerror="alert(1)">'
    state = {
        "nav0": 10000,
        "systems": {
            k: {"equity": 10000, "weights": {attack: 0.01}} for k in ["s1", "s2", "s3", "s4"]
        },
        "equity_history": {},
        "s4_attribution": [[attack, 0.01, 0.01, 1]],
        "last_actions": [attack],
        "data_incidents": [{"ts": "2026-10-09T18:00:00Z", "kind": attack, "detail": attack}],
        "ibkr": {
            "account": attack,
            "refresh_error": attack,
            "n_positions": attack,
            "positions": [{"symbol": attack, "shares": attack}],
            "fills": [
                {
                    "side": attack,
                    "shares": attack,
                    "symbol": attack,
                    "price": 1,
                    "time": "2026-10-09T18:00:00Z",
                }
            ],
        },
    }
    run_browser_regression(
        state,
        r"""
vm.runInContext('render(STATE)', context);
for (const id of ['chips', 'longs', 'attrib', 'activity', 'ibkHead', 'ibkKpis', 'ibkBody', 'ibkFills', 'ops']) {
  assert(!el(id).innerHTML.includes('<img'), id);
  assert(el(id).innerHTML.includes('&lt;img'), id);
}
vm.runInContext('LAST=STATE;openBook("s4")', context);
assert(!el('shPositions').innerHTML.includes('<img'));
""",
    )


def test_browser_refresh_labels_stale_and_failed_snapshots_honestly():
    state = {
        "nav0": 10000,
        "systems": {k: {"equity": 10000, "weights": {}} for k in ["s1", "s2", "s3", "s4"]},
    }
    run_browser_regression(
        state,
        r"""
context.STATE.last_tick = new Date().toISOString();
context.fetch = async () => ({ok: true, json: async () => context.STATE});
await vm.runInContext('load()', context);
assert.equal(el('dataStatus').textContent, 'LIVE');
context.STATE.last_tick = '2000-01-01T00:00:00Z';
await vm.runInContext('load()', context);
assert.equal(el('dataStatus').textContent, 'STALE');
context.fetch = async () => ({ok: false});
await vm.runInContext('load()', context);
assert.equal(el('dataStatus').textContent, 'OFFLINE · LAST SNAPSHOT');
vm.runInContext('LAST=null', context);
await vm.runInContext('load()', context);
assert.equal(el('dataStatus').textContent, 'DATA UNAVAILABLE');
context.fetch = async () => ({ok: true, json: async () => ({nav0: 'bad', systems: {}})});
await vm.runInContext('load()', context);
assert.equal(el('dataStatus').textContent, 'DATA UNAVAILABLE');
""",
    )


def test_browser_demo_flag_never_labels_a_fixture_live_even_when_recent():
    state = {"nav0": 10000, "demo_metadata": {"synthetic": True}, "systems": {}}
    run_browser_regression(
        state,
        r"""
context.STATE.last_tick = new Date().toISOString();
context.fetch = async () => ({ok: true, json: async () => context.STATE});
await vm.runInContext('load()', context);
assert.equal(el('dataStatus').textContent, 'DEMO · SYNTHETIC');
context.fetch = async () => ({ok: false});
await vm.runInContext('load()', context);
assert.equal(el('dataStatus').textContent, 'DEMO · OFFLINE · LAST SNAPSHOT');
""",
    )
