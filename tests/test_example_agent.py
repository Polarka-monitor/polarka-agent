"""The example agent's stamping loop (review 8, L7): it pays for at most two lots in one run, gives up after an hour,
and refuses an answer that is not a JSON object; a nonsense next_batch does not make it sleep for long."""
import importlib.util
import json
import os
import unittest

from polarka_agent.wallet import Refused

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("example_agent", os.path.join(HERE, "..", "examples", "agent_that_pays_for_itself.py"))
example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(example)
example.say = lambda *a, **k: None


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class Gate:
    """Answers like a gate that never gets the stamp done: each answer is a 402 paid for anew, or what it is given."""

    def __init__(self, answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, url, wallet, most, wait=600):
        self.asked.append(most)
        if not self.answers:
            raise AssertionError("asked once more than expected")
        a = self.answers.pop(0)
        if a == "pay":
            if most <= 0:
                raise Refused("X402_TOO_MUCH", "the resource asks more than allowed")
            return {"status": 200, "paid": "0.01", "txid": "tx%d" % len(self.asked), "calls_left": None,
                    "body": json.dumps({"state": "waiting", "next_batch": 0}).encode()}
        return a


class ExampleAgentTest(unittest.TestCase):
    def test_no_third_lot(self):
        clock = Clock()
        gate = Gate(["pay", "pay", "pay"])
        with self.assertRaises(SystemExit) as e:
            example.stamp("https://gate.example/v1/stamp/00", None, fetch=gate, sleep=clock.sleep, clock=clock)
        self.assertIn("2 lots paid", str(e.exception))
        self.assertEqual(gate.asked, [example.MOST, example.MOST, 0])

    def test_gives_up_after_an_hour(self):
        clock = Clock()
        waiting = {"status": 200, "paid": None, "calls_left": 90,
                   "body": json.dumps({"state": "waiting", "next_batch": clock.t + 10 ** 9}).encode()}
        gate = Gate([waiting] * 40)
        with self.assertRaises(SystemExit) as e:
            example.stamp("https://gate.example/v1/stamp/00", None, fetch=gate, sleep=clock.sleep, clock=clock)
        self.assertIn("within an hour", str(e.exception))
        self.assertEqual(set(clock.slept), {150})          # a next_batch years away is not believed
        self.assertLessEqual(len(gate.asked), 26)

    def test_refuses_a_body_that_is_not_an_object(self):
        clock = Clock()
        gate = Gate([{"status": 200, "paid": None, "body": b"[1, 2]"}])
        with self.assertRaises(SystemExit) as e:
            example.stamp("https://gate.example/v1/stamp/00", None, fetch=gate, sleep=clock.sleep, clock=clock)
        self.assertIn("not a JSON object", str(e.exception))

    def test_returns_the_proof(self):
        clock = Clock()
        done = {"status": 200, "paid": None, "calls_left": 97, "body": json.dumps({"state": "stamped", "txid": "t"}).encode()}
        gate = Gate(["pay", done])
        self.assertEqual(example.stamp("u", None, fetch=gate, sleep=clock.sleep, clock=clock)["state"], "stamped")


if __name__ == "__main__":
    unittest.main()
