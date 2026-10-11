import unittest

from helpers import A, B, NOW, offer, product, uid
from catalog import dbface, proposal

RUN = uid(99)


def cond(raw, provider=A):
    return {"raw": raw, "scope": "provider_scoped", "source_provider": provider,
            "title": raw.lower(), "details": None}


def entry(conditions=(), commission=None, provider=B, price=1000):
    rows = [product(uid(1), "Moto Halcon Nova", price, provider=provider, hours_ago=60)]
    commissions = [] if commission is None else [{"product_id": uid(1), **commission}]
    return dbface.load_catalog(rows, (), commissions, [
        {"id": uid(10 + n), "product_id": uid(1), "sort_order": n, **c}
        for n, c in enumerate(conditions)])[0]


def verdict(provider_changes=True, action="update"):
    return dbface.Verdict(item_key=f"{A}-001", provider_key=A, disposition="conflicto",
                          reason="x", detail="", action=action, product_id=uid(1),
                          provider_changes=provider_changes)


class Classify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(proposal.classify("🔧GARANTÍA: 15 DÍAS"), "warranty")
        self.assertEqual(proposal.classify("🚛TRANSPOTE INCLUIDO"), "transport")
        self.assertEqual(proposal.classify("🧾PAPELES EN REGLA"), "documents")
        self.assertEqual(proposal.classify("USD, EUR, MN, ZELLE"), "payment")

    def test_a_specification_is_not_published_as_a_payment_method(self):
        self.assertIsNone(proposal.classify("• Encendido inteligente por tarjeta magnética"))
        self.assertIsNone(proposal.classify("- transfer automático"))
        self.assertIsNone(proposal.classify("⛽️ EXTENSOR DE RANGO MECÁNICO DE 4500 WATTS"))

    def test_text_drops_decoration_and_the_announcing_label(self):
        self.assertEqual(proposal.term_text("warranty", "🔧GARANTÍA: 15 DÍAS"), "15 días")
        self.assertEqual(proposal.term_text("transport", "🚛TRANSPORTE INCLUIDO"), "Incluido")
        self.assertEqual(proposal.term_text("payment", "💲USD, EUR, MN, ZELLE"), "USD, EUR, MN, Zelle")
        self.assertEqual(proposal.term_text("payment", "💲FORMAS DE PAGO💲"), "")


class Terms(unittest.TestCase):
    def test_only_the_products_own_supplier_is_read(self):
        stated = proposal.stated_terms([cond("GARANTÍA: 1 AÑO", provider=B),
                                        cond("GARANTÍA: 15 DÍAS", provider=A)], A)
        self.assertEqual(stated, {"warranty": "15 días"})

    def test_header_plus_value_lines_become_one_term(self):
        stated = proposal.stated_terms([cond("💲FORMAS DE PAGO💲"), cond("USD, EUR, MN, ZELLE")], A)
        self.assertEqual(stated, {"payment": "USD, EUR, MN, Zelle"})

    def test_supplier_change_removes_terms_the_new_supplier_did_not_state(self):
        e = entry([{"icon": "shield", "title": "Garantía", "details": "1 año"},
                   {"icon": "truck", "title": "Transporte", "details": "Incluido"},
                   {"icon": "x", "title": "Colores", "details": "Rojo"}])
        row = proposal.build(RUN, verdict(), offer(f"{A}-001", "Moto Halcon Nova", 900),
                             [cond("TRANSPORTE INCLUIDO")], e, {})
        self.assertEqual(row["conditions_delete"], [uid(10)])       # the old warranty only
        self.assertNotIn("conditions_insert", row)                   # transport already says it
        self.assertNotIn(uid(12), row["conditions_delete"])          # colours are not commercial

    def test_same_supplier_keeps_terms_it_did_not_restate(self):
        e = entry([{"icon": "shield", "title": "Garantía", "details": "1 año"}], provider=A)
        row = proposal.build(RUN, verdict(provider_changes=False),
                             offer(f"{A}-001", "Moto Halcon Nova", 900), [], e, {})
        self.assertNotIn("conditions_delete", row)

    def test_inserted_term_has_a_deterministic_id_and_a_learned_icon(self):
        e = entry([])
        build = lambda: proposal.build(RUN, verdict(), offer(f"{A}-001", "Moto Halcon Nova", 900),
                                       [cond("GARANTÍA: 15 DÍAS")], e, {"warranty": "shield"})
        first, second = build(), build()
        self.assertEqual(first, second)
        inserted = first["conditions_insert"][0]
        self.assertEqual((inserted["title"], inserted["details"], inserted["icon"]),
                         ("Garantía", "15 días", "shield"))

    def test_icons_are_learned_from_the_catalog_not_declared(self):
        e = entry([{"icon": "shield", "title": "Garantía", "details": "x"},
                   {"icon": "truck", "title": "Mensajería", "details": "y"}])
        learned = proposal.learn_icons([e])
        self.assertEqual((learned["warranty"], learned["transport"], learned["payment"]),
                         ("shield", "truck", None))


class Offer(unittest.TestCase):
    def test_update_writes_only_price_and_supplier_columns(self):
        row = proposal.build(RUN, verdict(), offer(f"{A}-001", "Otro Nombre", 900), [], entry(), {})
        self.assertEqual(row["product"], {"base_price": 900.0, "provider_public_key": A})
        self.assertNotIn("details", row)

    def test_commission_of_the_old_supplier_does_not_survive_a_change(self):
        row = proposal.build(RUN, verdict(), offer(f"{A}-001", "Moto Halcon Nova", 900), [],
                             entry(commission={"value": 30, "notes": None}), {})
        self.assertTrue(row.get("commission_clear"))

    def test_free_commission_is_written_as_such(self):
        row = proposal.build(RUN, verdict(), offer(f"{A}-001", "Moto Halcon Nova", 900,
                                                   commission={"free": True}), [], entry(), {})
        self.assertEqual(row["commission"], {"commission_type": "fixed", "value": 0,
                                             "notes": proposal.FREE_COMMISSION_NOTE})

    def test_touch_carries_nothing(self):
        row = proposal.build(RUN, verdict(False, "touch"), offer(f"{A}-001", "x", 1), [], entry(), {})
        self.assertEqual(proposal.write_row(row), {"item_key": f"{A}-001", "product_id": uid(1),
                                                   "action": "touch"})

    def test_new_and_no_action_have_no_proposal(self):
        self.assertIsNone(proposal.build(RUN, verdict(False, "new"), {}, [], None, {}))
        self.assertIsNone(proposal.build(RUN, verdict(False, None), {}, [], entry(), {}))


if __name__ == "__main__":
    unittest.main()
