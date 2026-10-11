import unittest

from helpers import A, B, RULESET, uid
from catalog import validate


def good():
    return {"item_key": f"{A}-001", "product_id": uid(1), "action": "update",
            "product": {"base_price": 900.0, "provider_public_key": A},
            "commission": {"commission_type": "fixed", "value": 20, "notes": None},
            "conditions_delete": [uid(2)],
            "conditions_insert": [{"id": uid(3), "icon": "shield", "title": "Garantía",
                                   "details": "15 días", "sort_order": 0}]}


def gate(rows, **kw):
    return validate.gate(rows, ruleset=kw.pop("ruleset", RULESET), run_providers=[A, B],
                         item_providers={f"{A}-001": A, f"{A}-002": A}, **kw)


def codes(rows):
    return sorted({f.code for f in gate(rows).findings})


class Gate(unittest.TestCase):
    def test_a_good_row_passes(self):
        self.assertTrue(gate([good()]).ok)
        self.assertTrue(gate([{"item_key": f"{A}-001", "product_id": uid(1), "action": "touch"}]).ok)

    def test_creation_is_refused(self):
        self.assertEqual(codes([{"item_key": f"{A}-001", "action": "new"}]), ["no-creation"])

    def test_columns_outside_the_agents_contract_are_refused(self):
        row = good(); row["product"]["name"] = "otro"
        self.assertIn("product-unknown-key", codes([row]))
        row = good(); row["details"] = []
        self.assertIn("row-unknown-key", codes([row]))

    def test_publishing_under_a_supplier_that_did_not_win_is_refused(self):
        row = good(); row["product"]["provider_public_key"] = B
        self.assertIn("provider-mismatch", codes([row]))
        row = good(); row["product"]["provider_public_key"] = "PRV_ZZZZZZZZ"
        self.assertIn("provider-foreign", codes([row]))

    def test_limits_come_from_the_ruleset(self):
        row = good(); row["conditions_insert"][0]["details"] = "x" * 501
        self.assertIn("condition-details", codes([row]))
        row = good(); row["conditions_insert"][0]["icon"] = "not-an-icon"
        self.assertIn("condition-icon", codes([row]))
        row = good(); row["commission"]["value"] = -1
        self.assertIn("commission-value", codes([row]))
        row = good(); row["product"]["base_price"] = 0
        self.assertIn("price-invalid", codes([row]))

    def test_no_ruleset_is_a_refusal_not_a_pass(self):
        for broken in (None, {}, {"enums": {}, "limits": {}}):
            with self.assertRaises(validate.GateRefused):
                gate([good()], ruleset=broken)

    def test_two_rows_writing_one_product_are_refused(self):
        second = good(); second["item_key"] = f"{A}-002"
        self.assertIn("target-shared", codes([good(), second]))

    def test_touch_may_not_carry_data(self):
        self.assertIn("touch-carries-data", codes([{"item_key": f"{A}-001", "product_id": uid(1),
                                                    "action": "touch", "product": {"base_price": 1}}]))

    def test_refusals_are_per_item(self):
        bad = good(); bad["item_key"] = f"{A}-002"; bad["product_id"] = uid(5)
        bad["product"]["base_price"] = -3
        verdict = gate([good(), bad])
        self.assertEqual(verdict.refused_items(), frozenset({f"{A}-002"}))


class ToleranceDetector(unittest.TestCase):
    def test_positive_control(self):
        self.assertTrue(validate.price_tolerance_present("if price <= db_price * 1.10:"))
        self.assertTrue(validate.price_tolerance_present("PRICE_TOLERANCE = 3"))

    def test_the_engine_has_no_price_tolerance(self):
        from pathlib import Path
        import catalog
        for name in ("compare.py", "dbface.py", "proposal.py"):
            source = (Path(catalog.__file__).parent / name).read_text(encoding="utf-8")
            code = "\n".join(line.split("#")[0] for line in source.splitlines())
            # Docstrings may *name* the forbidden shape; executable lines may not contain it.
            import ast
            tree = ast.parse(source)
            doc = {id(n.body[0].value) for n in ast.walk(tree)
                   if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                   and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
                    text = ast.unparse(node)
                    self.assertFalse("price" in text.lower() and any(ch.isdigit() for ch in text),
                                     f"{name}: {text}")
            self.assertTrue(code)


if __name__ == "__main__":
    unittest.main()
