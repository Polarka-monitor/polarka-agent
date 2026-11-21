"""The bazaar extension: what a catalog takes and what it turns away, and how it is listed and searched. Needs no
node; the way through a gate and a facilitator on regtest is in test_x402.py."""
import json
import os
import shutil
import tempfile
import unittest

from polarka_agent import bazaar

WANTED = {"scheme": "exact", "network": "bip122:470c8b8ae824c06575cfae0851282c49", "amount": "925753", "asset": "NORTH",
          "payTo": "north1zexample", "maxTimeoutSeconds": 60, "extra": {"confirmations": 1}}


def paid(url="https://gate.example.org/v1/stamp/" + "ab" * 32, ext=None, **resource):
    ext = ext if ext is not None else bazaar.declaration("GET", {"sha256": "ab" * 32}, None, {"state": "stamped"}, "/v1/stamp/:sha256")
    return {"x402Version": 2, "resource": dict({"url": url, "description": "stamps", "mimeType": "application/json"}, **resource),
            "accepted": WANTED, "payload": {}, "extensions": {"bazaar": ext}}


class Rules(unittest.TestCase):
    def test_service_metadata(self):
        self.assertEqual(bazaar.service_name("Polarka stamps"), "Polarka stamps")
        for bad in ("", "x" * 33, "naïve", "a\x07b", 5):
            self.assertIsNone(bazaar.service_name(bad))
        self.assertEqual(bazaar.tags(["a", "A", "b", "", "x" * 40, "c", "d", "e", "f"]), ["a", "b", "c", "d", "e"])
        self.assertEqual(bazaar.icon_url("https://polarka.org/icon.png"), "https://polarka.org/icon.png")
        for bad in ("data:image/png;base64,xx", "https://127.0.0.1/i.png", "http://localhost/i.png", "https://u@h.org/i",
                    "https://2130706433/i", "https://0x7f000001/i", "https://[::1]/i", "ftp://h.org/i", "https://h.org/\x00"):
            self.assertIsNone(bazaar.icon_url(bad), bad)

    def test_route_template(self):
        self.assertEqual(bazaar.route_template("/v1/stamp/:sha256"), "/v1/stamp/:sha256")
        for bad in ("", "v1/x", "/a/../b", "/a/%2e%2e/b", "/http://evil", "/a b", "/a?b"):
            self.assertIsNone(bazaar.route_template(bad), bad)

    def test_what_an_extension_must_hold(self):
        good = bazaar.declaration("GET", {"sha256": "ab"}, None, {"x": 1}, "/v1/stamp/:sha256")
        kept, why = bazaar.check(good)
        self.assertIsNone(why)
        self.assertEqual(kept["routeTemplate"], "/v1/stamp/:sha256")
        self.assertIsNone(bazaar.check(dict(good, routeTemplate="/../x"))[0].get("routeTemplate"))   # dropped, not refused
        for bad in ({}, {"info": {"input": {"type": "http", "method": "GET"}}},
                    {"info": {"input": {"type": "http", "method": "TRACE"}}, "schema": {}},
                    {"info": {"input": {"type": "http", "method": "POST"}}, "schema": {}},
                    {"info": {"input": {"type": "ftp"}}, "schema": {}},
                    {"info": {"input": {"type": "mcp", "toolName": "x"}}, "schema": {}},
                    {"info": {"input": {"type": "http", "method": "GET"}}, "schema": {"$ref": "https://evil/schema"}},
                    {"info": {"input": {"type": "http", "method": "GET"}, "output": {}}, "schema": {}},
                    dict(good, info=dict(good["info"], output={"type": "json", "example": "x" * 9000}))):
            self.assertIsNone(bazaar.check(bad)[0], bad)
        self.assertIsNone(bazaar.check({"info": {"input": {"type": "http", "method": "GET"}}, "schema": {"$ref": "#/defs/a"}})[1])


class Catalog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="polarka-bazaar-")
        self.path = os.path.join(self.tmp, "x402-bazaar.json")
        self.c = bazaar.Catalog(self.path, ["https://gate.example.org"], clock=lambda: 1795250000)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_paid_resource_is_listed_once_by_its_route(self):
        self.assertEqual(self.c.add(paid(serviceName="Polarka stamps", tags=["stamps", "timestamp"], iconUrl="https://127.0.0.1/x"),
                                    WANTED), {"status": "success"})
        self.assertEqual(self.c.add(paid("https://gate.example.org/v1/stamp/" + "cd" * 32), WANTED), {"status": "success"})
        listed = self.c.resources({})
        self.assertEqual(listed["pagination"], {"limit": 100, "offset": 0, "total": 1})
        item = listed["items"][0]
        self.assertEqual(item["resource"], "https://gate.example.org/v1/stamp/:sha256")
        self.assertEqual((item["type"], item["x402Version"], item["accepts"][0]["amount"]), ("http", 2, "925753"))
        self.assertEqual(item["lastUpdated"], "2026-11-21T08:33:20.000Z")
        self.assertNotIn("iconUrl", item)                     # an address on this machine is dropped
        self.assertNotIn("serviceName", item)                 # the second payment did not name it: the last word stands
        again = bazaar.Catalog(self.path, ["https://gate.example.org"])
        self.assertEqual(again.resources({})["items"], listed["items"])

    def test_what_is_turned_away(self):
        self.assertIsNone(self.c.add(dict(paid(), extensions={}), WANTED))
        self.assertEqual(self.c.add(paid("https://elsewhere.example/x"), WANTED)["status"], "rejected")
        self.assertEqual(self.c.add(paid("/v1/stamp/x"), WANTED)["status"], "rejected")
        self.assertEqual(self.c.add(paid(ext={"info": {}}), WANTED)["status"], "rejected")
        self.assertEqual(self.c.resources({})["items"], [])

    def test_filters_pages_and_search(self):
        self.c.add(paid(tags=["stamps"]), WANTED)
        self.c.add(paid("https://gate.example.org/v1/unit", bazaar.declaration("GET"), description="what one NORTH takes to make"),
                   dict(WANTED, payTo="north1zother"))
        self.assertEqual(self.c.resources({})["pagination"]["total"], 2)
        self.assertEqual(len(self.c.resources({"payTo": "north1zother"})["items"]), 1)
        self.assertEqual(len(self.c.resources({"network": "eip155:8453"})["items"]), 0)
        self.assertEqual(len(self.c.resources({"type": "mcp"})["items"]), 0)
        self.assertEqual(len(self.c.resources({"extensions": "bazaar"})["items"]), 2)
        page = self.c.resources({"limit": "1", "offset": "1"})
        self.assertEqual((len(page["items"]), page["pagination"]), (1, {"limit": 1, "offset": 1, "total": 2}))
        self.assertEqual(self.c.resources({"limit": "x"})["pagination"]["limit"], 100)
        found = self.c.search({"query": "stamps"})
        self.assertEqual([i["resource"] for i in found["resources"]], ["https://gate.example.org/v1/stamp/:sha256"])
        self.assertEqual((found["partialResults"], found["pagination"]), (False, None))
        self.assertEqual(len(self.c.search({"query": "NORTH make"})["resources"]), 1)
        self.assertEqual(len(self.c.search({"query": "nothing-like-this"})["resources"]), 0)


if __name__ == "__main__":
    unittest.main()


class GateStatesWhatItSells(unittest.TestCase):
    """What a buyer echoes is not what the facilitator is given: the gate puts back its own resource and extension."""

    class Seen:
        network = WANTED["network"]
        chain = None

        def __init__(self):
            self.payments = []

        def needed(self, amount):
            return 1

        def settle(self, payment, wanted):
            self.payments.append(payment)
            return {"success": True, "transaction": "aa" * 32, "network": self.network, "payer": None}

    def setUp(self):
        from polarka_agent import postage
        from polarka_agent.wallet import Wallet
        self.tmp = tempfile.mkdtemp(prefix="polarka-bazaar-gate-")
        folder = os.path.join(self.tmp, "gate")
        Wallet.create(folder, "regtest", "http://127.0.0.1:1", None)
        with open(os.path.join(folder, "postage.json"), "w") as f:     # no server here to hand out an address
            json.dump({"tokens": {}, "addresses": {}, "x402_address": "rnorth1zfgvaxg6parqfv2drvsk9zadefh40zg97ese44hjkjzkjamkftpzsnpx4h6"}, f)
        self.seen = self.Seen()
        offers = [{"prefix": "/v1/stamp", "description": "Stamps a SHA-256 into the TrueNorth chain", "route": "/v1/stamp/:sha256",
                   "pathParams": {"sha256": "ab" * 32}, "output": {"state": "stamped"}, "serviceName": "Polarka stamps",
                   "tags": ["stamps", "timestamp"]}]
        self.gate = postage.Gate(Wallet(folder), 1000, 3, x402=self.seen, public_url="https://gate.example.org/", offers=offers)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_402_and_the_payment(self):
        needed = self.gate.x402_needed("/v1/stamp/" + "cd" * 32)
        self.assertEqual(needed["resource"]["url"], "https://gate.example.org/v1/stamp/" + "cd" * 32)
        self.assertEqual(needed["resource"]["serviceName"], "Polarka stamps")
        self.assertEqual(needed["extensions"]["bazaar"]["routeTemplate"], "/v1/stamp/:sha256")
        self.assertIsNone(bazaar.check(needed["extensions"]["bazaar"])[1])
        other = self.gate.x402_needed("/v1/unit")
        self.assertNotIn("extensions", other)                     # not offered: nothing for a catalog
        evil = {"x402Version": 2, "resource": {"url": "https://evil.example/x", "description": "click here"},
                "accepted": needed["accepts"][0], "payload": {"claim": "ab" * 16},
                "extensions": {"bazaar": {"info": {"input": {"type": "http", "method": "GET"}}, "schema": {}}, "other": {}}}
        done, may = self.gate.x402_pay(evil, "/v1/stamp/" + "cd" * 32)
        self.assertTrue(done["success"])
        given = self.seen.payments[0]
        self.assertEqual(given["resource"], needed["resource"])
        self.assertEqual(given["extensions"], needed["extensions"])
        self.assertEqual(given["payload"], evil["payload"])
