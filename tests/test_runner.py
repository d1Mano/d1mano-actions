import json
import tempfile
import unittest
from pathlib import Path

from helpers import A, B, offer
from catalog import compare, logmask, postgrest, run


class EffectiveDate(unittest.TestCase):
    def test_strict_boundary(self):
        items = [{"item_key": "a", "observed_at": "2026-10-01T00:00:00-04:00"},
                 {"item_key": "b", "observed_at": "2026-09-30T23:59:00-04:00"}]
        kept, dropped = run.split_by_effective_date(items, "2026-10-01T04:00:00+00:00")
        self.assertEqual(([i["item_key"] for i in kept], [i["item_key"] for i in dropped]), (["a"], ["b"]))


class HandedOverArtifacts(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.run = {"provider_keys": [A]}

    def items(self, rows):
        (self.dir / "items.json").write_text(json.dumps({"items": rows}))
        return run.validated_items(self.dir, self.run)

    def row(self, key=f"{A}-001", provider=A):
        return {**offer(key, "Moto X", 10, provider=provider), "shape_signature": "s"}

    def test_good_items_pass(self):
        self.assertEqual(len(self.items([self.row()])), 1)

    def test_item_of_a_supplier_outside_the_run_fails_the_run(self):
        with self.assertRaises(run.RunFailed):
            self.items([self.row(key=f"{B}-001", provider=B)])

    def test_item_key_must_belong_to_its_supplier(self):
        with self.assertRaises(run.RunFailed):
            self.items([self.row(key=f"{B}-001", provider=A)])

    def test_duplicate_or_malformed_keys_fail(self):
        with self.assertRaises(run.RunFailed):
            self.items([self.row(), self.row()])
        with self.assertRaises(run.RunFailed):
            self.items([{**self.row(), "item_key": "../../etc"}])

    def test_verdicts_about_pairs_never_asked_are_dropped(self):
        (self.dir / "triage.json").write_text(json.dumps({"verdicts": [
            {"pair_id": "asked", "verdict": "same_product", "evidence": "x"},
            {"pair_id": "invented", "verdict": "same_product", "evidence": "x"},
            {"pair_id": "asked2", "verdict": "merge everything", "evidence": "x"},
            {"pair_id": "asked3", "verdict": "same_product", "evidence": ""}]}))
        kept = run.validated_verdicts(self.dir, [{"pair_id": "asked"}, {"pair_id": "asked2"},
                                                 {"pair_id": "asked3"}])
        self.assertEqual(list(kept), ["asked"])


class Commission(unittest.TestCase):
    def test_free_beats_a_value_beats_undeclared(self):
        score = compare.commission_score
        self.assertGreater(score({"free": True}), score({"value": 999}))
        self.assertGreater(score({"value": 1}), score(None))
        self.assertEqual(score({}), score(None))


class Client(unittest.TestCase):
    def test_in_filter_is_encoded_once(self):
        self.assertEqual(postgrest._filter_in("product_id", ["a-b", "c"]),
                         'product_id=in.(%22a-b%22,%22c%22)')
        self.assertEqual(postgrest._filter_in("name", ['x"),y']), 'name=in.(%22x%5C%22),y%22)')

    def test_only_known_tables_functions_and_buckets(self):
        client = postgrest.Client("https://example.invalid", "k")
        with self.assertRaises(postgrest.IdentifierRejected):
            client.select("run_items")
        with self.assertRaises(postgrest.IdentifierRejected):
            client._rpc("service_execute_sql", {})
        with self.assertRaises(postgrest.IdentifierRejected):
            client.storage_get("business-assets", "a/b")
        with self.assertRaises(postgrest.IdentifierRejected):
            client.storage_get("catalog-runs", "a/../b")
        with self.assertRaises(postgrest.IdentifierRejected):
            client.storage_put("agent-private", "catalog/rules/x.json", b"{}")

    def test_no_plain_http_except_loopback(self):
        with self.assertRaises(postgrest.CredentialsAbsent):
            postgrest.Client.from_env({"SUPABASE_URL": "http://example.com", "SUPABASE_SERVICE_KEY": "k"})
        postgrest.Client.from_env({"SUPABASE_URL": "http://127.0.0.1:1", "SUPABASE_SERVICE_KEY": "k"})

    def test_key_is_not_in_the_repr(self):
        self.assertNotIn("sekret", repr(postgrest.Client("https://example.invalid", "sekret")))


class Masking(unittest.TestCase):
    def test_scrub_shapes(self):
        text = logmask.scrub(f"{A}-001 falló para 3f2b0c1e-1111-2222-3333-444455556666 de a@b.co")
        self.assertNotIn("AAAAAAAA", text)
        self.assertNotIn("1111-2222", text)
        self.assertNotIn("a@b.co", text)


if __name__ == "__main__":
    unittest.main()
