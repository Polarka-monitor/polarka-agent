"""The remote MCP server (connector.py): the protocol, the tools with stand-ins for the stamp service and the pool,
the free stamps and their bounds, what is kept in --dir, and the rules of the HTTP endpoint."""
import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from polarka_agent import connector
from polarka_agent.httpguard import Bounded, Limiter

DAY = 86400
T0 = 1795392000                 # 2026-11-23 00:00:00 UTC


def digest(n):
    return "%064x" % (n + 1)


class Clock:
    def __init__(self, t=T0 + 3600):
        self.t = t

    def __call__(self):
        return self.t


class Services:
    """The stamp service and the pool of this machine, as stand-ins on the loopback."""

    def __init__(self):
        self.stamped, self.asked, self.stamp_error, self.stats_asked = set(), [], None, 0
        self.stats = {"coin": "NORTH", "network": "main", "t": T0 + 3500,
                      "pool": {"hashrate": 5729, "miners": 1, "fee_percent": 5, "payout_scheme": "PPS", "min_payout": "1.00000000",
                               "blocks_found": 28},
                      "chain": {"height": 30570, "blocks_24h": 721, "hashrate": 342576}}
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/v1/stats":
                    me.stats_asked += 1
                    return self.send(200, me.stats)
                self.send(404, {"error": "NOT_HERE"})

            def do_POST(self):
                d = self.path[len("/v1/stamp/"):]
                me.asked.append(d)
                if me.stamp_error:
                    return self.send(*me.stamp_error)
                if d in me.stamped:
                    return self.send(200, {"version": 1, "digest": d, "state": "stamped", "root": "ab" * 32, "path": [],
                                           "txid": "cd" * 32, "transaction": "00", "block_hash": "ef" * 32, "block_height": 30580,
                                           "confirmations": 3, "txoutproof": "00", "rules": "..."})
                self.send(200, {"digest": d, "state": "waiting", "next_batch": T0 + 4200, "then": "ask again after the next batch and one block"})

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.http.server_address[1]

    def stop(self):
        self.http.shutdown()
        self.http.server_close()


def call(app, name, arguments=None, where="198.51.100.7", ident=1):
    message = {"jsonrpc": "2.0", "id": ident, "method": "tools/call", "params": {"name": name}}
    if arguments is not None:
        message["params"]["arguments"] = arguments
    got = app.answer(message, where)
    result = got["result"]
    return json.loads(result["content"][0]["text"]), result["isError"], result


class Protocol(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.services = Services()
        self.clock = Clock()
        self.app = connector.Connector(self.folder, self.services.url, self.services.url, None, 5, 3, self.clock)

    def tearDown(self):
        self.services.stop()
        shutil.rmtree(self.folder)

    def test_initialize_names_a_version_it_speaks(self):
        # 2025-03-26 is not spoken since review 12 (I1): its servers take batches; the client is offered the newest
        for wanted, got in (("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"), ("2025-03-26", "2025-11-25"),
                            ("1999-01-01", "2025-11-25"), (None, "2025-11-25")):
            r = self.app.answer({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": wanted, "clientInfo": {"name": "claude-ai<script>"}}}, "x")["result"]
            self.assertEqual(r["protocolVersion"], got)
            self.assertEqual(r["capabilities"], {"tools": {"listChanged": False}})
            self.assertEqual(r["serverInfo"]["name"], "polarka-connector")
            self.assertIn("never send the data", r["instructions"])
        self.app.save()
        with open(os.path.join(self.folder, "usage.json")) as f:
            day = json.load(f)["days"]["2026-11-23"]
        self.assertEqual(day["initialize"], 5)
        # counted under a name of the list, never the caller's own text (review 12, I8)
        self.assertEqual(day["clients"], {"claude": 5})

    def test_the_tools_say_what_they_do_and_change(self):
        tools = self.app.answer({"jsonrpc": "2.0", "id": "a", "method": "tools/list"}, "x")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["north_network", "north_guide", "north_stamp", "north_stamp_verify"])
        for t in tools:
            self.assertFalse(t["inputSchema"]["additionalProperties"])
            self.assertIn("readOnlyHint", t["annotations"])
            self.assertGreater(len(t["description"]), 150)
        self.assertEqual([t["name"] for t in tools if not t["annotations"]["readOnlyHint"]], ["north_stamp"])

    def test_messages_that_are_no_request(self):
        self.assertIsNone(self.app.answer({"jsonrpc": "2.0", "method": "notifications/initialized"}, "x"))
        self.assertIsNone(self.app.answer({"jsonrpc": "2.0", "id": 4, "result": {}}, "x"))
        self.assertEqual(self.app.answer({"id": 1, "method": "ping"}, "x")["error"]["code"], -32600)
        self.assertEqual(self.app.answer([], "x")["error"]["code"], -32600)
        for ident in (True, None, 1.5, [1], {"a": 1}):
            self.assertEqual(self.app.answer({"jsonrpc": "2.0", "id": ident, "method": "ping"}, "x")["error"]["code"], -32600, ident)
        self.assertEqual(self.app.answer({"jsonrpc": "2.0", "id": 2, "method": "ping"}, "x")["result"], {})
        self.assertEqual(self.app.answer({"jsonrpc": "2.0", "id": 2, "method": "resources/list"}, "x")["error"]["code"], -32601)
        self.assertEqual(self.app.answer({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": []}, "x")["error"]["code"], -32602)

    def test_an_unknown_tool_or_argument(self):
        got = self.app.answer({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "north_pay"}}, "x")
        self.assertEqual(got["error"]["code"], -32602)
        shown, failed, _ = call(self.app, "north_guide", {"topic": "start", "extra": 1})
        self.assertTrue(failed)
        self.assertEqual(shown["error"], "INPUT")
        shown, failed, _ = call(self.app, "north_guide", ["start"])
        self.assertTrue(failed)
        self.app.save()
        with open(os.path.join(self.folder, "usage.json")) as f:
            calls = json.load(f)["days"]["2026-11-23"]["calls"]
        self.assertEqual(calls, {"north_guide": 2})          # the name of a tool that does not exist is not written down

    def test_the_guide(self):
        shown, failed, result = call(self.app, "north_guide")
        self.assertFalse(failed)
        self.assertEqual(shown["topic"], "start")
        self.assertEqual(result["structuredContent"], shown)
        self.assertIn("not the TrueNorth developers", " ".join(shown["text"]))
        for topic in connector.GUIDE:
            shown, failed, _ = call(self.app, "north_guide", {"topic": topic})
            self.assertFalse(failed)
            self.assertTrue(all(link.startswith("https://") for link in shown["links"]))
            self.assertNotIn("severka-monitor", json.dumps(shown))
        shown, failed, _ = call(self.app, "north_guide", {"topic": "price"})
        self.assertTrue(failed)

    def test_the_network(self):
        shown, failed, _ = call(self.app, "north_network")
        self.assertFalse(failed, shown)
        # the pool names the block it mines, 30570: the chain's tip is one below it (review 12, L2)
        self.assertEqual(shown["chain"]["height"], 30569)
        self.assertEqual(shown["chain"]["hashrate_estimate"], "342.6 kH/s")
        self.assertEqual(shown["chain"]["block_reward_north"], 512)
        self.assertEqual(shown["chain"]["issued_north"], 512 * 30569)
        self.assertNotIn("market", shown)
        self.assertEqual(shown["chain"]["next_halving_height"], 1051200)
        self.assertEqual(shown["pool"]["fee_percent"], 5)
        self.assertEqual(shown["as_of"], "2026-11-23T00:58:20Z")
        call(self.app, "north_network")
        self.assertEqual(self.services.stats_asked, 1)            # kept for half a minute
        self.clock.t += 31
        call(self.app, "north_network")
        self.assertEqual(self.services.stats_asked, 2)

    def test_the_network_when_the_pool_is_silent(self):
        app = connector.Connector(self.folder, self.services.url, "http://127.0.0.1:9", None, 5, 3, self.clock)
        shown, failed, _ = call(app, "north_network")
        self.assertTrue(failed)
        self.assertEqual(shown["error"], "UNAVAILABLE")
        self.services.stats = {"chain": "nothing"}
        shown, failed, _ = call(self.app, "north_network")
        self.assertTrue(failed)

    def test_reward_and_issued(self):
        self.assertEqual([connector.reward(h) for h in (1, 1051199, 1051200, 2102400, 6307200, 7358400, 10 ** 9)],
                         [512, 512, 256, 128, 8, 8, 8])
        self.assertEqual(connector.issued(0), 0)
        self.assertEqual(connector.issued(30570), 512 * 30570)
        self.assertEqual(connector.issued(1051201), 512 * 1051199 + 256 * 2)


class FreeStamps(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.services = Services()
        self.clock = Clock()
        self.app = connector.Connector(self.folder, self.services.url, self.services.url, None, 5, 3, self.clock)

    def tearDown(self):
        self.services.stop()
        shutil.rmtree(self.folder)

    def test_a_stamp_waits_then_comes_with_its_proof(self):
        shown, failed, _ = call(self.app, "north_stamp", {"sha256": digest(1).upper()})
        self.assertFalse(failed, shown)
        self.assertEqual((shown["state"], shown["digest"], shown["free_stamps_left_today_for_all_callers"]), ("waiting", digest(1), 4))
        self.assertEqual(shown["next_batch_utc"], "2026-11-23T01:10:00Z")
        self.services.stamped.add(digest(1))
        shown, failed, _ = call(self.app, "north_stamp", {"sha256": digest(1)})
        self.assertEqual((shown["state"], shown["block_height"], shown["free_stamps_left_today_for_all_callers"]), ("stamped", 30580, 4))
        self.assertEqual(shown["proof"]["txid"], "cd" * 32)

    def test_bad_input_costs_nothing(self):
        for bad in ("12", digest(1) + "0", "g" * 64, 5, None):
            shown, failed, _ = call(self.app, "north_stamp", {"sha256": bad})
            self.assertTrue(failed)
            self.assertEqual(shown["error"], "INPUT")
        shown, failed, _ = call(self.app, "north_stamp", {})
        self.assertTrue(failed)
        self.assertEqual(self.services.asked, [])

    def test_the_bounds_of_a_place_and_of_the_day(self):
        for n in range(3):
            self.assertFalse(call(self.app, "north_stamp", {"sha256": digest(n)}, where="a")[1])
        shown, failed, _ = call(self.app, "north_stamp", {"sha256": digest(3)}, where="a")
        self.assertEqual((failed, shown["error"]), (True, "ENOUGH_TODAY"))
        self.assertFalse(call(self.app, "north_stamp", {"sha256": digest(0)}, where="a")[1])   # taken before: free
        self.assertFalse(call(self.app, "north_stamp", {"sha256": digest(3)}, where="b")[1])
        self.assertFalse(call(self.app, "north_stamp", {"sha256": digest(4)}, where="b")[1])
        shown, failed, _ = call(self.app, "north_stamp", {"sha256": digest(5)}, where="c")
        self.assertEqual((failed, shown["error"]), (True, "ENOUGH_TODAY"))
        self.assertIn("tomorrow", shown["message"])
        self.assertEqual(len(self.services.asked), 6)

    def test_a_refusal_of_the_service_gives_the_free_stamp_back(self):
        self.services.stamp_error = (503, {"error": "BUSY", "message": "the next batch is full; ask again after 1795396200"})
        shown, failed, _ = call(self.app, "north_stamp", {"sha256": digest(1)})
        self.assertEqual((failed, shown["error"]), (True, "BUSY"))
        app = connector.Connector(self.folder, "http://127.0.0.1:9", self.services.url, None, 5, 3, self.clock)
        shown, failed, _ = call(app, "north_stamp", {"sha256": digest(2)})
        self.assertEqual((failed, shown["error"]), (True, "UNAVAILABLE"))
        self.services.stamp_error = None
        shown, failed, _ = call(self.app, "north_stamp", {"sha256": digest(1)})
        self.assertEqual(shown["free_stamps_left_today_for_all_callers"], 4)
        self.services.stamp_error = (200, {"digest": digest(9), "state": "waiting"})       # an answer about another hash
        shown, failed, _ = call(self.app, "north_stamp", {"sha256": digest(3)})
        self.assertTrue(failed)
        self.services.stamp_error = None
        self.assertEqual(call(self.app, "north_stamp", {"sha256": digest(4)})[0]["free_stamps_left_today_for_all_callers"], 3)

    def test_what_is_kept_over_a_restart_and_a_new_day(self):
        for n in range(3):
            call(self.app, "north_stamp", {"sha256": digest(n)}, where="a")
        app = connector.Connector(self.folder, self.services.url, self.services.url, None, 5, 3, self.clock)
        self.assertEqual(call(app, "north_stamp", {"sha256": digest(0)}, where="a")[0]["free_stamps_left_today_for_all_callers"], 2)
        self.assertEqual(call(app, "north_stamp", {"sha256": digest(7)}, where="a")[0]["free_stamps_left_today_for_all_callers"], 1)
        self.clock.t += DAY
        shown = call(app, "north_stamp", {"sha256": digest(8)}, where="a")[0]
        self.assertEqual(shown["free_stamps_left_today_for_all_callers"], 4)
        self.assertEqual(call(app, "north_stamp", {"sha256": digest(0)}, where="a")[0]["free_stamps_left_today_for_all_callers"], 4)
        self.clock.t += 8 * DAY
        self.assertEqual(call(app, "north_stamp", {"sha256": digest(0)}, where="a")[0]["free_stamps_left_today_for_all_callers"], 4)
        with open(os.path.join(self.folder, "state.json")) as f:
            kept = json.load(f)
        self.assertEqual(set(kept["known"]), {digest(0)})          # older than a week: forgotten
        self.assertEqual(set(kept), {"v", "day", "free", "known"})          # no place is written down


class Verify(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.seen = []

    def tearDown(self):
        shutil.rmtree(self.folder)

    def app(self, node, verify=None):
        return connector.Connector(self.folder, "http://127.0.0.1:9", "http://127.0.0.1:9", node, 5, 3, Clock(), verify)

    def test_a_proof_that_does_not_hold(self):
        shown, failed, _ = call(self.app(object()), "north_stamp_verify", {"proof": {"digest": digest(1)}})
        self.assertFalse(failed)
        self.assertEqual(shown, {"valid": False, "reason": "this is not a proof of a stamp"})
        shown, failed, _ = call(self.app(object()), "north_stamp_verify", {"proof": "{}"})
        self.assertEqual((failed, shown["error"]), (True, "INPUT"))

    def test_a_proof_that_holds(self):
        def verify(proof, node):
            self.seen.append(node)
            return {"path": True, "root_in_transaction": True, "transaction_in_block": True, "block": True,
                    "confirmations": 12, "time": T0}
        node = object()
        proof = {"digest": digest(1), "txid": "cd" * 32, "block_hash": "ef" * 32, "block_height": 30580}
        shown, failed, _ = call(self.app(node, verify), "north_stamp_verify", {"proof": proof})
        self.assertFalse(failed)
        self.assertEqual((shown["valid"], shown["confirmations"], shown["block_time_utc"]), (True, 12, "2026-11-23T00:00:00Z"))
        self.assertEqual(shown["checked"], ["block", "path", "root_in_transaction", "transaction_in_block"])
        self.assertIs(self.seen[0], node)

    def test_without_a_node_a_proof_is_not_called_valid(self):
        shown, failed, _ = call(self.app(None, lambda p, n: {"path": True, "block": "not checked"}), "north_stamp_verify",
                                {"proof": {"digest": digest(1)}})
        self.assertIsNone(shown["valid"])
        self.assertIn("no node was asked", shown["note"])

    def test_a_node_that_cannot_be_asked(self):
        def verify(proof, node):
            raise ConnectionRefusedError()
        shown, failed, _ = call(self.app(object(), verify), "north_stamp_verify", {"proof": {"digest": digest(1)}})
        self.assertEqual((failed, shown["error"]), (True, "UNAVAILABLE"))


class Endpoint(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.app = connector.Connector(self.folder, "http://127.0.0.1:9", "http://127.0.0.1:9", None, 5, 3, Clock())
        self.limiter = Limiter(600)
        self.http = Bounded(("127.0.0.1", 0), connector.make_handler(self.app, False, self.limiter))
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.port = self.http.server_address[1]

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        shutil.rmtree(self.folder)

    def ask(self, method, path="/mcp", body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        sent.update(headers or {})
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        c.request(method, path, body=data, headers={k: v for k, v in sent.items() if v is not None})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, dict(r.getheaders()), (json.loads(raw) if raw else None)

    def test_a_request_gets_json_and_a_notification_202(self):
        status, headers, got = self.ask("POST", body={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                      "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual((status, headers["Content-Type"], headers["Access-Control-Allow-Origin"]), (200, "application/json", "*"))
        self.assertEqual(got["result"]["protocolVersion"], "2025-11-25")
        self.assertNotIn("Mcp-Session-Id", headers)
        status, headers, got = self.ask("POST", "/mcp/", {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                        {"MCP-Protocol-Version": "2025-11-25"})
        self.assertEqual((status, got, headers["Content-Length"]), (202, None, "0"))
        status, _, got = self.ask("POST", body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                  headers={"MCP-Protocol-Version": "2025-06-18", "Origin": "https://claude.ai"})
        self.assertEqual((status, len(got["result"]["tools"])), (200, 4))

    def test_no_stream_no_session(self):
        status, headers, got = self.ask("GET", headers={"Accept": "text/event-stream"})
        self.assertEqual((status, headers["Allow"]), (405, "POST, OPTIONS"))
        self.assertEqual(self.ask("DELETE")[0], 405)
        status, headers, _ = self.ask("OPTIONS", headers={"Origin": "https://example.org", "Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 204)
        self.assertIn("MCP-Protocol-Version", headers["Access-Control-Allow-Headers"])
        self.assertEqual(self.ask("GET", "/")[0], 404)
        self.assertEqual(self.ask("POST", "/mcpx", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0], 404)
        self.assertEqual(self.ask("GET", "/.well-known/oauth-protected-resource")[0], 404)

    def test_what_is_refused_before_it_is_read(self):
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        for origin in ("null", "file:///etc", "https://a.example/path", "not a url"):
            status, _, got = self.ask("POST", body=ping, headers={"Origin": origin})
            self.assertEqual(status, 403, origin)
        self.assertEqual(self.ask("POST", body=ping, headers={"Origin": "http://localhost:6274"})[0], 200)
        status, _, got = self.ask("POST", body=ping, headers={"MCP-Protocol-Version": "2024-11-05"})
        self.assertEqual((status, got["error"]["code"]), (400, -32600))
        self.assertEqual(self.ask("POST", body=ping, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.ask("POST", body=b"x" * (connector.MOST_BODY + 1))[0], 413)
        status, _, got = self.ask("POST", body=b"{not json")
        self.assertEqual((status, got["error"]["code"]), (400, -32700))
        status, _, got = self.ask("POST", body=b"[" * 100000 + b"]" * 100000)
        self.assertEqual(status, 413)
        status, _, got = self.ask("POST", body=b"[" * 30000 + b"]" * 30000)
        # too deep for this Python's reader (-32700), or read and then refused as a batch (-32600): never an answer
        self.assertEqual(status, 400)
        self.assertIn(got["error"]["code"], (-32700, -32600))
        status, _, got = self.ask("POST", body=[ping, ping])
        self.assertEqual((status, got["error"]["code"]), (400, -32600))
        self.assertEqual(self.ask("POST", body=ping)[0], 200)

    def test_requests_a_minute_from_one_place(self):
        self.limiter.per_minute, self.limiter.buckets = 1, type(self.limiter.buckets)()
        self.limiter.everyone, self.limiter.all = 10, (10.0, self.limiter.clock())
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        statuses = [self.ask("POST", body=ping)[0] for _ in range(3)]
        self.assertEqual(statuses[0], 200)
        self.assertIn(429, statuses[1:])


class CommandLine(unittest.TestCase):
    def test_only_the_loopback_and_services_of_this_machine(self):
        from polarka_agent import cli
        folder = tempfile.mkdtemp()
        try:
            for extra in (["--listen", "0.0.0.0:8696"], ["--stamps", "https://example.org"], ["--pool", "http://10.0.0.1:8690"],
                          ["--rpc-url", "http://127.0.0.1:9554"], ["--free-per-place", "9", "--free-per-day", "5"], ["--per-minute", "0"]):
                out = io.StringIO()
                status = cli.run(["--dir", folder, "connector-serve"] + extra, out)
                self.assertEqual(status, 2, extra)
                self.assertEqual(json.loads(out.getvalue())["error"], "INPUT", extra)
        finally:
            shutil.rmtree(folder)


if __name__ == "__main__":
    unittest.main()
