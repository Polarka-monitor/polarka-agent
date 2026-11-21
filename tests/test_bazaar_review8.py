"""What review 8 found in the bazaar (M3, L2, L3), each as a test that failed before the fix."""
import json
import os
import shutil
import tempfile
import time
import unittest

from polarka_agent import bazaar
from polarka_agent.postage import load_offers
from polarka_agent.wallet import Refused

from tests.test_bazaar import WANTED, paid


class Filters(unittest.TestCase):
    def test_this_machine_in_other_spellings_is_no_icon(self):
        for bad in ("https://127.1/i", "https://0177.0.0.1/i", "https://0x7f.0.0.1/i", "https://2130706433./i",
                    "https://localhost./i", "https://foo.localhost/i", "https://10.1/i"):
            self.assertIsNone(bazaar.icon_url(bad), bad)
        self.assertEqual(bazaar.icon_url("https://polarka.org/i.png"), "https://polarka.org/i.png")

    def test_a_route_that_would_leave_the_origin(self):
        self.assertIsNone(bazaar.route_template("//evil.example/x"))
        self.assertEqual(bazaar.route_template("/v1/stamp/:sha256"), "/v1/stamp/:sha256")

    def test_schema_keywords_that_point_elsewhere(self):
        ext = bazaar.declaration("GET")
        for key, value in (("$dynamicRef", "https://evil.example/s"), ("$recursiveRef", "https://evil.example/s"),
                           ("$schema", "https://evil.example/s"), ("$schema", {"x": 1})):
            bad = json.loads(json.dumps(ext))
            bad["schema"][key] = value
            kept, why = bazaar.check(bad)
            self.assertIsNone(kept, key)
        kept, why = bazaar.check(ext)
        self.assertIsNotNone(kept, why)


class SearchCostsLittle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="polarka-bazaar8-")
        self.c = bazaar.Catalog(os.path.join(self.tmp, "c.json"), ["https://gate.example.org"], clock=lambda: 1795250000)
        for n in range(200):
            self.c.add(paid("https://gate.example.org/v1/r%d" % n, bazaar.declaration("GET", route="/v1/r%d" % n),
                            description="d" * 400), WANTED)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_query_of_many_words(self):
        started = time.monotonic()
        self.c.search({"query": " ".join(["a"] * 32000) + " zz"})
        self.c.search({"query": " ".join("w%d" % i for i in range(5000))})
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_page_is_at_most_a_hundred(self):
        self.assertEqual(self.c.resources({"limit": "1000"})["pagination"]["limit"], 100)
        self.assertEqual(len(self.c.resources({"limit": "1000"})["items"]), 100)


class OffersNeedARoute(unittest.TestCase):
    def offers(self, obj):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        try:
            return load_offers(path)
        finally:
            os.unlink(path)

    def test_without_a_route_or_outside_its_prefix(self):
        for o in ({"prefix": "/v1/stamp", "description": "x"},
                  {"prefix": "/v1/stamp", "description": "x", "route": "/v1/other/:a"},
                  {"prefix": "/v1/stamp", "description": "x", "route": "//evil.example/v1/stamp"}):
            with self.assertRaises(Refused):
                self.offers([o])
        self.assertEqual(len(self.offers([{"prefix": "/v1/stamp", "description": "x", "route": "/v1/stamp/:sha256"}])), 1)


if __name__ == "__main__":
    unittest.main()
