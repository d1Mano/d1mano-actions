import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from helpers import A, B
from catalog import compare, dates, ingest, whatsapp

ZONE = ZoneInfo("America/Havana")
REF = datetime(2026, 10, 11, 6, 0, tzinfo=timezone.utc)

PASTED = """[10/10 9:29 a. m.] +53 5 0000001: `🌀VENTILADOR RECARGABLE X1🌀`
▸ Batería de 20,000mAh
💳 USD, EUR, MN, ZELLE

💵 Precio: $55
💰 Comisión: $5
[10/10 10:31 a. m.] +53 5 0000001: `BICIMOTO ALFA`
• 48 Volt
PRECIO: $720
COMISIÓN: $20
[10/10 4:05 p. m.] +53 5 0000001: `BICIMOTO ALFA`
• 48 Volt
PRECIO: $700
COMISIÓN: $20
[10/10 4:24 p. m.] +53 5 0000001: *🛺 TRICICLO BETA PREMIUM 🛺*
☑️Motor de 1200w
PRECIO : 3650 USD
COMISIÓN : 60 USD

---------------------------------------
Con extensor de Rango

Precio : 3950 USD
Comion : 65 USD
[10/10 5:00 p. m.] +53 5 0000001: 💥REBAJA DE PRESIO💥

TRICICLO GAMA 2026  🤑 DISPONIBLE

Precio 3630 USD
Comision : 50

Presio: 3580
Comision: 25
[10/10 5:55 p. m.] +53 5 0000001: Estación delta clasicc
3600 w pico
600 USD (10USD COMISIÓN)
Mensajería incluida
[10/10 5:57 p. m.] +53 5 0000001: 55(4 usd comisión)x unidad

✍🏼cable de 6 metros
[10/10 6:00 p. m.] +53 5 0000001: buenas tardes grupo, mañana llegan más
"""


def ingest_text(text, provider=A, reference=REF, name="00.txt"):
    tmp = Path(tempfile.mkdtemp())
    (tmp / name).write_text(text, encoding="utf-8")
    spec = ingest.SourceSpec(provider_key=provider, path=tmp / name, label="")
    return ingest.ingest_source(spec, 0, tmp / "work", ZONE, reference)


class PastedFormat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records, cls.report = ingest_text(PASTED)
        cls.by_price = {r["price"]: r for r in cls.records}

    def test_every_priced_message_is_a_product_and_chatter_is_not(self):
        self.assertEqual(sorted(self.by_price), [55.0, 600.0, 700.0, 720.0, 3580.0, 3630.0, 3650.0, 3950.0])
        self.assertEqual(len(self.records), 9)   # two things cost 55

    def test_year_comes_from_the_runs_reference_not_from_today(self):
        self.assertTrue(all(r["observed_at"].startswith("2026-10-10T") for r in self.records))
        self.assertEqual(self.by_price[720.0]["observed_at"], "2026-10-10T10:31:00-04:00")
        self.assertEqual(self.by_price[700.0]["observed_at"], "2026-10-10T16:05:00-04:00")

    def test_names_drop_decoration_and_skip_promotional_headers(self):
        self.assertEqual(self.by_price[720.0]["name"], "BICIMOTO ALFA")
        self.assertEqual(self.by_price[3580.0]["name"], "TRICICLO GAMA 2026")
        names = {r["name"] for r in self.records}
        self.assertIn("VENTILADOR RECARGABLE X1", names)
        self.assertIn("cable de 6 metros", names)

    def test_variant_announced_on_its_own_line_gets_its_own_name(self):
        self.assertEqual(self.by_price[3650.0]["name"], "TRICICLO BETA PREMIUM")
        self.assertEqual(self.by_price[3950.0]["name"], "TRICICLO BETA PREMIUM con Extensor de Rango")

    def test_each_price_carries_the_commission_stated_with_it(self):
        self.assertEqual(self.by_price[3630.0]["commission"]["value"], 50.0)
        self.assertEqual(self.by_price[3580.0]["commission"]["value"], 25.0)
        self.assertEqual(self.by_price[3950.0]["commission"]["value"], 65.0)   # "Comion"

    def test_price_without_the_word_precio(self):
        self.assertEqual(self.by_price[600.0]["name"], "Estación delta clasicc")
        self.assertEqual(self.by_price[600.0]["commission"]["value"], 10.0)
        cable = [r for r in self.records if r["name"] == "cable de 6 metros"][0]
        self.assertEqual((cable["price"], cable["commission"]["value"]), (55.0, 4.0))

    def test_a_spec_line_starting_with_a_number_is_not_a_price(self):
        self.assertNotIn(3600.0, self.by_price)

    def test_sender_phone_is_not_kept(self):
        self.assertNotIn("0000001", json.dumps(self.records))

    def test_one_shape_and_deterministic_keys(self):
        again, _ = ingest_text(PASTED)
        self.assertEqual([r["item_key"] for r in again], [r["item_key"] for r in self.records])
        self.assertEqual(len({r["shape_signature"] for r in self.records}), 1)


class OneOfferPerProductPerSupplier(unittest.TestCase):
    def test_latest_announcement_wins_and_within_it_the_cheapest(self):
        records, _ = ingest_text(PASTED)
        result = compare.run(records, {})
        dup = {k: v for k, v in result.dispositions.items()
               if v["disposition"] == compare.DUPLICATE}
        by_key = {r["item_key"]: r for r in records}
        dropped = sorted(by_key[k]["price"] for k in dup)
        self.assertEqual(dropped, [720.0, 3630.0])       # the morning price; the dearer tier
        kept = sorted(by_key[k]["price"] for k in result.merged)
        self.assertIn(700.0, kept)
        self.assertIn(3580.0, kept)
        self.assertIn(3650.0, kept)                      # base and "con extensor" both stay
        self.assertIn(3950.0, kept)
        self.assertEqual(result.funnel["parsed"], 9)
        self.assertEqual(len(result.merged) + len(dup), 9)


class Dates(unittest.TestCase):
    def stamp(self, line):
        fields, _, style = dates.match_header(line)
        return dates.Stamp(line, *fields, sender="", line_number=1), style

    def test_both_copied_shapes_and_the_export_shape(self):
        self.assertEqual(self.stamp("[10/10 9:29 a. m.] x: y")[1], dates.STYLE_COPIED)
        s, style = self.stamp("[9:29 p. m., 5/10/2026] Ana: hola")
        self.assertEqual((s.first, s.second, s.year_two_digit, s.hour, s.meridiem, style),
                         (5, 10, 26, 9, "p", dates.STYLE_COPIED))
        self.assertEqual(self.stamp("1/10/26, 8:43 p. m. - Ana: hola")[1], dates.STYLE_EXPORT)
        self.assertIsNone(dates.match_header("• 48 Volt"))

    def test_a_pasted_block_inside_an_exported_message_uses_the_inner_stamp(self):
        s, style = self.stamp("11/10/26, 2:00 a. m. - Yo: [10/10 9:29 a. m.] +53 5 1: `X`")
        self.assertEqual((s.first, s.second, s.hour, style), (10, 10, 9, dates.STYLE_COPIED))

    def test_december_message_pasted_in_january_is_last_year(self):
        s, _ = self.stamp("[30/12 9:00 a. m.] x: y")
        moment = dates.to_datetime(s, dates.DAY_FIRST, datetime(2027, 1, 3, 12, 0))
        self.assertEqual((moment.year, moment.month, moment.day), (2026, 12, 30))

    def test_no_year_and_no_reference_is_refused(self):
        s, _ = self.stamp("[10/10 9:00 a. m.] x: y")
        with self.assertRaises(dates.DateRejected):
            dates.to_datetime(s, dates.DAY_FIRST, None)

    def test_order_by_proof_by_elimination_and_by_declared_default(self):
        ref = datetime(2026, 10, 11, 6, 0)
        proof = dates.resolve_order([self.stamp("[25/9 9:00 a. m.] x: y")[0]], ref)
        self.assertEqual((proof.order, proof.fallback), (dates.DAY_FIRST, ""))
        # With its year stated, 11/9/2026 read month-first is 9 November: after the
        # reference, so it cannot be. (Without a year it could be last November, and
        # the declared default decides instead.)
        eliminated = dates.resolve_order([self.stamp("[9:00 a. m., 11/9/2026] x: y")[0]],
                                         datetime(2026, 10, 11, 6, 0))
        self.assertEqual(eliminated.order, dates.DAY_FIRST)
        self.assertIn("future", eliminated.fallback)
        default = dates.resolve_order([self.stamp("[5/6 9:00 a. m.] x: y")[0]], ref)
        self.assertEqual(default.order, dates.DAY_FIRST)
        self.assertIn("declared", default.fallback)

    def test_two_locales_in_one_paste_are_refused(self):
        with self.assertRaises(dates.DateRejected):
            dates.resolve_order([self.stamp("[25/9 9:00 a. m.] x: y")[0],
                                 self.stamp("[9/25 9:00 a. m.] x: y")[0]], datetime(2026, 10, 11))

    def test_text_that_is_not_a_chat_is_refused(self):
        with self.assertRaises(ingest.IngestRejected):
            ingest_text("hola, estos son mis productos\nprecio: 100\n")


if __name__ == "__main__":
    unittest.main()
