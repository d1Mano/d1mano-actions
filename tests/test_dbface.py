import unittest

from helpers import A, B, NOW, offer, product, uid
from catalog import dbface


def run(items, products, commissions=(), details=()):
    catalog = dbface.load_catalog(products, details, commissions)
    return {v.item_key: v for v in dbface.confront(items, catalog, now=NOW).verdicts}


class Identity(unittest.TestCase):
    def test_different_measurement_in_name_is_not_the_same_product(self):
        out = run([offer(f"{A}-001", "Sistema Solar 12kw", 5800)],
                  [product(uid(1), "Sistema Solar 6kw", 4500, provider=B, hours_ago=500)])
        verdict = out[f"{A}-001"]
        self.assertEqual(verdict.disposition, dbface.INEXISTENTE)
        self.assertEqual(verdict.action, "new")
        self.assertEqual(verdict.near_product_id, uid(1))

    def test_same_number_with_and_without_unit_is_one_measurement(self):
        self.assertIsNone(dbface.specs_disagree(("planta", "110", "4500"), ("planta", "110v", "4500w")))

    def test_one_sided_measurement_is_not_a_disagreement(self):
        self.assertIsNone(dbface.specs_disagree(("moto", "x"), ("moto", "x", "1000w")))

    def test_no_similar_name_goes_back_to_the_client(self):
        out = run([offer(f"{A}-001", "Lavadora Automática", 300)],
                  [product(uid(1), "Triciclo de Carga", 3000)])
        self.assertEqual(out[f"{A}-001"].action, "new")

    def test_one_catalog_product_is_never_the_target_of_two_items(self):
        out = run([offer(f"{A}-001", "Moto Halcon Nova", 1000),
                   offer(f"{A}-002", "Moto Halcon Nova", 1100)],
                  [product(uid(1), "Moto Halcon Nova", 1000)])
        targets = [v.product_id for v in out.values() if v.action in ("update", "touch")]
        self.assertEqual(len(targets), 1)
        # The cheaper rival takes the row (R-026). The other is not a new product:
        # the product exists and a better offer of this same run was chosen for it.
        self.assertEqual(out[f"{A}-001"].action, "touch")
        loser = out[f"{A}-002"]
        self.assertEqual((loser.action, loser.reason, loser.skip_reason),
                         (None, "outbid_in_run", "CATALOG_OFFER_KEPT"))


class NameIdentity(unittest.TestCase):
    GENERIC = frozenset({"moto", "electrica", "triciclo", "electrico", "bicimoto", "topmaq"})

    def same(self, left, right):
        score, _ = dbface.name_identity(dbface.identity_words(left),
                                        dbface.identity_words(right), self.GENERIC)
        return score is not None

    def test_an_omission_is_not_a_difference(self):
        self.assertTrue(self.same("TRICICLO ELÉCTRICO KVITOVA PREMIUM", "Triciclo Eléctrico Kvitova"))
        self.assertTrue(self.same("Planta marca Pulsar", "Planta Eléctrica Pulsar 10000W"))
        self.assertTrue(self.same("BICICLETA ELÉCTRICA MARCA ONEBOT 2026", "Bicicleta Eléctrica Onebot"))

    def test_a_contradiction_is(self):
        self.assertFalse(self.same("MOTO ELÉCTRICA TOPMAQ TANK", "Moto Eléctrica Topmaq F3"))
        self.assertFalse(self.same("MOTICO ORIS", "MOTICO MAF"))
        self.assertFalse(self.same("BICIMOTO JMD 2026", "Patineta Eléctrica JMD"))
        self.assertFalse(self.same("MOTO ELÉCTRICA TANK", "Moto Automática Tank"))
        self.assertFalse(self.same("Bicicleta Eléctrica A12", "Bicicleta Eléctrica A9"))

    def test_sharing_only_generic_words_identifies_nothing(self):
        self.assertFalse(self.same("MOTO ELÉCTRICA TOPMAQ", "Moto Eléctrica Topmaq F3"))

    def test_misspellings_are_one_word_but_similar_words_are_not(self):
        self.assertTrue(self.same("BICIMOTO YOAZAKY 707", "Bicimoto Yoazaki 707"))
        self.assertTrue(self.same("MOTO DE GASOLINA KATZU SNAPDRAGON", "Moto de Gasolina Katsu Snapdragon"))
        self.assertTrue(self.same("TRICICLO TRACKTION", "Triciclo Traktion 2026"))
        self.assertFalse(self.same("Cuatriciclo eléctrico Bryderk", "Triciclo Eléctrico Bryderk"))
        self.assertFalse(self.same("BICIMOTICO ELÉCTRICA JMD", "Bicimoto Eléctrica JMD"))
        self.assertTrue(self.same("MOTICO PRONTEZZA", "Motico Protenzza"))

    def test_a_short_name_inside_a_long_one_is_not_enough(self):
        self.assertFalse(self.same(
            "Inversor dos en uno Must de 3kw y batería de 7kwh", "Batería Marca Must"))

    def test_add_on_in_one_name_only_is_another_product(self):
        out = run([offer(f"{A}-001", "Triciclo Jinpeng con cabina", 4680)],
                  [product(uid(1), "Triciclo Jinpeng", 3900, provider=B, hours_ago=90)])
        self.assertEqual(out[f"{A}-001"].action, "new")

    def test_rivals_for_one_row_are_settled_by_price(self):
        out = run([offer(f"{A}-001", "Moto Halcon Nova", 1100),
                   offer(f"{B}-001", "Moto Halcon Nova", 1000, provider=B)],
                  [product(uid(1), "Moto Halcon Nova", 1200, provider="PRV_CCCCCCCC", hours_ago=5)])
        self.assertEqual(out[f"{B}-001"].action, "update")
        self.assertEqual(out[f"{A}-001"].reason, "outbid_in_run")


class SameSupplier(unittest.TestCase):
    def test_unchanged_offer_is_a_refresh(self):
        v = run([offer(f"{A}-001", "Moto Halcon Nova", 1000)],
                [product(uid(1), "Moto Halcon Nova", 1000, hours_ago=200)])[f"{A}-001"]
        self.assertEqual((v.disposition, v.action, v.reason), ("existente", "touch", "unchanged"))

    def test_supplier_raising_its_own_price_is_an_update_not_a_refresh(self):
        v = run([offer(f"{A}-001", "Moto Halcon Nova", 1200)],
                [product(uid(1), "Moto Halcon Nova", 1000)])[f"{A}-001"]
        self.assertEqual((v.action, v.reason, v.provider_changes), ("update", "provider_repriced", False))

    def test_supplier_moving_its_own_price_a_lot_waits_for_the_owner(self):
        v = run([offer(f"{A}-001", "Moto Halcon Nova", 700)],
                [product(uid(1), "Moto Halcon Nova", 1000)])[f"{A}-001"]
        self.assertEqual((v.action, v.needs_review), ("update", True))

    def test_absurd_price_move_is_not_applied(self):
        v = run([offer(f"{A}-001", "Moto Halcon Nova", 14)],
                [product(uid(1), "Moto Halcon Nova", 1400)])[f"{A}-001"]
        self.assertIsNone(v.action)
        self.assertEqual(v.skip_reason, "IMPLAUSIBLE_PRICE")


class OtherSupplier(unittest.TestCase):
    def test_fresh_better_catalog_offer_is_kept_and_not_refreshed(self):
        # Refreshing it on another supplier's evidence would keep it from ever going stale.
        v = run([offer(f"{A}-001", "Moto Halcon Nova", 1100)],
                [product(uid(1), "Moto Halcon Nova", 1000, provider=B, hours_ago=5)])[f"{A}-001"]
        self.assertIsNone(v.action)
        self.assertEqual(v.skip_reason, "CATALOG_OFFER_KEPT")

    def test_cheaper_offer_wins(self):
        v = run([offer(f"{A}-001", "Moto Halcon Nova", 950)],
                [product(uid(1), "Moto Halcon Nova", 1000, provider=B, hours_ago=5)])[f"{A}-001"]
        self.assertEqual((v.action, v.reason, v.provider_changes, v.needs_review),
                         ("update", "better_offer", True, False))

    def test_stale_catalog_offer_yields_even_to_a_worse_price(self):
        v = run([offer(f"{A}-001", "Moto Halcon Nova", 1100)],
                [product(uid(1), "Moto Halcon Nova", 1000, provider=B, hours_ago=60)])[f"{A}-001"]
        self.assertEqual((v.action, v.reason), ("update", "stale_yielded"))

    def test_47_hours_is_not_stale(self):
        v = run([offer(f"{A}-001", "Moto Halcon Nova", 1100)],
                [product(uid(1), "Moto Halcon Nova", 1000, provider=B, hours_ago=47)])[f"{A}-001"]
        self.assertIsNone(v.action)

    def test_undated_catalog_product_is_unknown_not_stale(self):
        row = product(uid(1), "Moto Halcon Nova", 1000, provider=B)
        row["created_at"] = row["updated_at"] = None
        v = run([offer(f"{A}-001", "Moto Halcon Nova", 1100)], [row])[f"{A}-001"]
        self.assertIsNone(v.action)

    def test_commission_only_breaks_an_exact_price_tie(self):
        rows = [product(uid(1), "Moto Halcon Nova", 1000, provider=B, hours_ago=5)]
        commissions = [{"product_id": uid(1), "value": 20, "notes": None}]
        tie = run([offer(f"{A}-001", "Moto Halcon Nova", 1000, commission={"value": 50})],
                  rows, commissions)[f"{A}-001"]
        self.assertEqual((tie.action, tie.reason), ("update", "commission_tie"))
        dearer = run([offer(f"{A}-001", "Moto Halcon Nova", 1001, commission={"value": 500})],
                     rows, commissions)[f"{A}-001"]
        self.assertIsNone(dearer.action, "a commission must never buy a worse price")

    def test_undeclared_commission_does_not_win_a_tie(self):
        rows = [product(uid(1), "Moto Halcon Nova", 1000, provider=B, hours_ago=5)]
        commissions = [{"product_id": uid(1), "value": 20, "notes": None}]
        v = run([offer(f"{A}-001", "Moto Halcon Nova", 1000)], rows, commissions)[f"{A}-001"]
        self.assertIsNone(v.action)

    def test_big_gap_between_suppliers_is_proposed_but_flagged(self):
        v = run([offer(f"{A}-001", "Moto Halcon Nova", 600)],
                [product(uid(1), "Moto Halcon Nova", 1000, provider=B, hours_ago=5)])[f"{A}-001"]
        self.assertEqual(v.action, "update")
        self.assertTrue(v.needs_review)

    def test_offer_without_price_cannot_compete(self):
        v = run([offer(f"{A}-001", "Moto Halcon Nova", None)],
                [product(uid(1), "Moto Halcon Nova", 1000, provider=B, hours_ago=90)])[f"{A}-001"]
        self.assertIsNone(v.action)


class Funnel(unittest.TestCase):
    def test_every_input_has_exactly_one_verdict(self):
        items = [offer(f"{A}-{n:03d}", f"Producto {chr(64 + n)} Uno", 100 + n) for n in range(1, 9)]
        catalog = dbface.load_catalog([product(uid(1), "Producto A Uno", 101)])
        result = dbface.confront(items, catalog, now=NOW)
        self.assertEqual(result.funnel["resolved"], 8)
        self.assertEqual(len({v.item_key for v in result.verdicts}), 8)

    def test_repeated_item_key_is_refused(self):
        with self.assertRaises(dbface.ConfrontationRejected):
            dbface.confront([offer(f"{A}-001", "X Y", 1), offer(f"{A}-001", "X Y", 1)], (), now=NOW)

    def test_same_snapshot_same_verdicts(self):
        items = [offer(f"{A}-001", "Moto Halcon Nova", 1100)]
        rows = [product(uid(1), "Moto Halcon Nova", 1000, provider=B, hours_ago=60)]
        catalog = dbface.load_catalog(rows)
        first = dbface.confront(items, catalog, now=NOW).as_dict()
        second = dbface.confront(items, catalog, now=NOW).as_dict()
        self.assertEqual(first, second)


class Staleness(unittest.TestCase):
    def test_variable_width_fraction_parses(self):
        self.assertIsNotNone(dbface.product_stale_hours(None, "2026-01-01T00:00:00.96898+00:00", NOW))

    def test_touch_resets_the_clock(self):
        self.assertEqual(dbface.product_effective_date("2026-01-01", "2026-02-01"), "2026-02-01")
        self.assertEqual(dbface.product_effective_date("2026-01-01", "2026-01-01"), "2026-01-01")


if __name__ == "__main__":
    unittest.main()
