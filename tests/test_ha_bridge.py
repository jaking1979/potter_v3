import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "overlay/tools"))
import ha_bridge as hb  # noqa: E402

TOKEN = "-".join(["fake", "bridge", "value", "for", "tests"])     # built at runtime: not a real credential


class Resp:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.body[:n] if n and n > 0 else self.body


class T(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.env = Path(self._t.name) / "ha.env"
        self.env.write_text(f'HA_URL=http://ha.example:8123/\nHA_TOKEN="{TOKEN}"\n')
        self.seen = []
        self.reply = {"state": "heat", "attributes": {"temperature": 62}, "last_updated": "t", "context": {"x": 1}}

    def opener(self, req, timeout=None):
        self.seen.append((req.get_method(), req.full_url, req.data, req.get_header("Authorization"), timeout))
        if isinstance(self.reply, Exception):
            raise self.reply
        return Resp(self.reply)

    def run_op(self, op):
        out = io.StringIO()
        code = hb.main(["--env", str(self.env)], stdin=io.StringIO(json.dumps(op) if not isinstance(op, str) else op),
                       stdout=out, opener=self.opener)
        return code, json.loads(out.getvalue()), out.getvalue()

    def test_get_state_returns_only_the_contract_fields_and_sends_the_token_in_the_header(self):
        code, ans, raw = self.run_op({"op": "get_state", "entity_id": "climate.main_house"})
        self.assertEqual((code, ans["ok"]), (0, True))
        self.assertEqual(sorted(ans["state"]), ["attributes", "last_updated", "state"])
        method, url, _, auth, timeout = self.seen[0]
        self.assertEqual((method, url, auth), ("GET", "http://ha.example:8123/api/states/climate.main_house", "Bearer " + TOKEN))
        self.assertEqual(timeout, hb.TIMEOUT_S)
        self.assertNotIn(TOKEN, raw)

    def test_a_missing_entity_is_null_not_an_error(self):
        self.reply = urllib.error.HTTPError("u", 404, "nf", {}, None)
        self.assertEqual(self.run_op({"op": "get_state", "entity_id": "sensor.gone"})[1], {"ok": True, "state": None})

    def test_call_service_posts_the_data(self):
        self.reply = []
        code, ans, _ = self.run_op({"op": "call_service", "domain": "climate", "service": "set_temperature",
                                    "data": {"entity_id": "climate.main_house", "temperature": 64}})
        self.assertEqual((code, ans), (0, {"ok": True}))
        method, url, data, _, _ = self.seen[0]
        self.assertEqual((method, url), ("POST", "http://ha.example:8123/api/services/climate/set_temperature"))
        self.assertEqual(json.loads(data)["temperature"], 64)

    def test_history_returns_state_and_time_rows(self):
        self.reply = [[{"state": "61.5", "last_changed": "a", "attributes": {}}, {"state": "61.0", "last_changed": "b"}]]
        code, ans, _ = self.run_op({"op": "history", "entity_id": "sensor.basement_temperature", "start": "2026-11-16T00:00:00+00:00"})
        self.assertEqual(ans["rows"], [{"state": "61.5", "last_changed": "a"}, {"state": "61.0", "last_changed": "b"}])
        self.assertIn("filter_entity_id=sensor.basement_temperature", self.seen[0][1])

    def test_bad_requests_are_refused_before_any_network_call(self):
        for op in ({"op": "get_state", "entity_id": "../etc/passwd"}, {"op": "get_state", "entity_id": "Climate.X"},
                   {"op": "call_service", "domain": "climate", "service": "set_temperature", "data": {}},
                   {"op": "call_service", "domain": "a/b", "service": "x", "data": {"entity_id": "climate.x"}},
                   {"op": "history", "entity_id": "sensor.x", "start": "yesterday; rm -rf"},
                   {"op": "delete_everything"}, [1, 2], "not json"):
            code, ans, _ = self.run_op(op)
            self.assertEqual((code, ans), (1, {"ok": False, "error": "bad_request"}), op)
        self.assertEqual(self.seen, [])

    def test_failures_are_short_codes_and_never_contain_the_token_or_address(self):
        for exc, code_name in ((urllib.error.HTTPError("u", 401, "x", {}, None), "unauthorized"),
                               (urllib.error.HTTPError("u", 500, "x", {}, None), "http_500"),
                               (OSError(f"cannot reach http://ha.example:8123 with {TOKEN}"), "unreachable")):
            self.reply = exc
            code, ans, raw = self.run_op({"op": "get_state", "entity_id": "climate.x"})
            self.assertEqual((code, ans), (1, {"ok": False, "error": code_name}))
            self.assertNotIn(TOKEN, raw)
            self.assertNotIn("ha.example", raw)
        self.reply = b"<html>"
        self.assertEqual(self.run_op({"op": "get_state", "entity_id": "climate.x"})[1]["error"], "bad_reply")
        self.reply = b"x" * (hb.MAX_BYTES + 5)
        self.assertEqual(self.run_op({"op": "get_state", "entity_id": "climate.x"})[1]["error"], "too_large")

    def test_missing_or_incomplete_config_is_not_configured(self):
        for text in ("", "HA_URL=http://ha.example:8123\n", "HA_TOKEN=x\n", "HA_URL=ftp://x\nHA_TOKEN=x\n"):
            self.env.write_text(text)
            self.assertEqual(self.run_op({"op": "get_state", "entity_id": "climate.x"})[1]["error"], "not_configured")
        self.env.unlink()
        self.assertEqual(self.run_op({"op": "get_state", "entity_id": "climate.x"})[1]["error"], "not_configured")


if __name__ == "__main__":
    unittest.main()


class EndToEnd(unittest.TestCase):
    """Merrick's safety layer -> bridge command -> a Home Assistant-shaped HTTP server on localhost."""

    def test_merrick_sets_a_temperature_through_the_real_bridge_with_read_back(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        sys.path.insert(0, str(ROOT / "overlay"))
        from merrick.ha import SafeHome
        from merrick.habridge import BridgeHomeAssistant
        states = {"climate.main_house": {"state": "heat", "attributes": {"temperature": 62.0}, "last_updated": "t"}}
        auth = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                auth.append(self.headers.get("Authorization"))
                eid = self.path.rsplit("/", 1)[-1]
                self._send(200, states[eid]) if eid in states else self._send(404, {})

            def do_POST(self):
                auth.append(self.headers.get("Authorization"))
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                states[body["entity_id"]]["attributes"]["temperature"] = body["temperature"]
                self._send(200, [])

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        with tempfile.TemporaryDirectory() as t:
            env = Path(t) / "ha.env"
            env.write_text(f"HA_URL=http://127.0.0.1:{srv.server_address[1]}\nHA_TOKEN={TOKEN}\n")
            client = BridgeHomeAssistant(f"{sys.executable} {ROOT / 'overlay/tools/ha_bridge.py'} --env {env}")
            house = {"temperature_unit": "F", "zones": {"main": {"climate_entity": "climate.main_house"}}}
            home = SafeHome(client, house, sleep=lambda s: None)
            res = home.set_temperature("climate.main_house", 80)
            self.assertTrue(res.ok, res.message)
            self.assertEqual((res.applied, res.clamped), (74.0, True))
            self.assertEqual(states["climate.main_house"]["attributes"]["temperature"], 74.0)
            self.assertIsNone(home.read("sensor.does_not_exist"))
            self.assertEqual(home.lock("lock.front_door", confirmed=True).outcome, "refused")
        self.assertTrue(all(a == "Bearer " + TOKEN for a in auth))
