import unittest

from helpers import A, RULESET, uid
from catalog import apply
from catalog.postgrest import DatabaseRefused


class FakeClient:
    """Records the calls in order and can be told to refuse a product or die at a leg."""

    def __init__(self, refuse=(), die_at=None, stopped=False):
        self.calls, self.refuse, self.die_at, self.stopped = [], set(refuse), die_at, stopped
        self.products, self.stamps, self.done, self.failed = {}, {}, set(), {}

    def _maybe_die(self, leg):
        if self.die_at == leg:
            self.die_at = None
            raise ConnectionError(f"killed at {leg}")

    def apply_products(self, run_id, rows):
        self.calls.append(("apply", [r["item_key"] for r in rows]))
        if self.stopped:
            raise DatabaseRefused("stopped", code="U0207")
        if any(r["product_id"] in self.refuse for r in rows):
            raise DatabaseRefused("nope", code="U0196", detail="not in tenant")
        self._maybe_die("apply")
        for r in rows:
            self.products[r["product_id"]] = self.products.get(r["product_id"], 0) + 1

    def stamp_applies(self, run_id, rows):
        self.calls.append(("stamp", [r["item_key"] for r in rows]))
        self._maybe_die("stamp")
        for r in rows:
            self.stamps[r["item_key"]] = r["product_id"]

    def mark_done(self, run_id, rows):
        self.calls.append(("done", [r["item_key"] for r in rows]))
        self._maybe_die("done")
        for r in rows:
            assert r["item_key"] in self.stamps, "DONE without a stamp"
            self.done.add(r["item_key"])

    def advance_items(self, run_id, rows):
        self.calls.append(("fail", [r["item_key"] for r in rows]))
        for r in rows:
            self.failed[r["item_key"]] = r["patch"]["failure"]

    def claimed(self, n):
        return [{"item_key": f"{A}-{i:03d}", "provider_key": A,
                 "applied_at": "x" if f"{A}-{i:03d}" in self.stamps and f"{A}-{i:03d}" not in self.done else None,
                 "payload": {"proposal": {"item_key": f"{A}-{i:03d}", "product_id": uid(i),
                                          "action": "update", "product": {"base_price": 10.0 + i},
                                          "changes": [{"field": "base_price"}]}}}
                for i in range(1, n + 1) if f"{A}-{i:03d}" not in self.done]


def go(client, items):
    return apply.apply_batch(client, "run", items, ruleset=RULESET, run_providers=[A])


class Legs(unittest.TestCase):
    def test_order_is_write_then_stamp_then_done(self):
        client = FakeClient()
        go(client, client.claimed(3))
        self.assertEqual([c[0] for c in client.calls], ["apply", "stamp", "done"])
        self.assertEqual(len(client.done), 3)

    def test_changes_never_travel_to_the_database(self):
        seen = []
        client = FakeClient()
        client.apply_products = lambda run_id, rows: seen.extend(rows)
        go(client, client.claimed(1))
        self.assertNotIn("changes", seen[0])

    def test_one_refused_row_fails_alone(self):
        client = FakeClient(refuse={uid(2)})
        outcome = go(client, client.claimed(3))
        self.assertEqual(sorted(outcome.written), [f"{A}-001", f"{A}-003"])
        self.assertEqual(list(outcome.failed), [f"{A}-002"])
        self.assertEqual(client.done, {f"{A}-001", f"{A}-003"})
        self.assertNotIn(f"{A}-002", client.stamps, "a stamp on an unwritten row suppresses the replay")

    def test_gate_refusal_is_not_sent(self):
        client = FakeClient()
        items = client.claimed(2)
        items[1]["payload"]["proposal"]["product"]["base_price"] = -5
        outcome = go(client, items)
        self.assertEqual(outcome.written, [f"{A}-001"])
        self.assertIn(f"{A}-002", client.failed)
        self.assertNotIn(uid(2), client.products)

    def test_item_without_a_proposal_fails_instead_of_vanishing(self):
        client = FakeClient()
        items = client.claimed(1)
        items[0]["payload"] = {}
        self.assertEqual(list(go(client, items).failed), [f"{A}-001"])

    def test_cancelled_run_stops_the_slice(self):
        client = FakeClient(stopped=True)
        with self.assertRaises(apply.RunStopped):
            go(client, client.claimed(2))


class Replay(unittest.TestCase):
    def test_cut_between_stamp_and_done_does_not_write_twice(self):
        client = FakeClient(die_at="done")
        with self.assertRaises(ConnectionError):
            go(client, client.claimed(3))
        self.assertEqual(client.done, set())
        outcome = go(client, client.claimed(3))            # the next slice
        self.assertEqual(len(outcome.resumed), 3)
        self.assertEqual(set(client.products.values()), {1}, "each product written exactly once")
        self.assertEqual(len(client.done), 3)

    def test_cut_between_write_and_stamp_replays_the_write_and_converges(self):
        client = FakeClient(die_at="stamp")
        with self.assertRaises(ConnectionError):
            go(client, client.claimed(2))
        go(client, client.claimed(2))
        # Written twice, which is why every write is idempotent: a price is set, not added.
        self.assertEqual(set(client.products.values()), {2})
        self.assertEqual(len(client.done), 2)


if __name__ == "__main__":
    unittest.main()
