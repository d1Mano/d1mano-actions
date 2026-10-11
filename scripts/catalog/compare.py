"""Step 2: one list out of several vendors' lists, and nobody disappears (R-023..R-027).

The database is not in this file. R-023 puts the comparison between vendors *before and
apart from* the confrontation, using only what the client sent. That boundary is not
cosmetic: a vendor's terms and a database row are different kinds of claim, and mixing
them is how the legacy pipeline ended up deciding a product's identity from a snapshot
taken three days earlier. Nothing here opens a socket.

Three questions, three different owners. Collapsing them into one score is what produced
a list of 149 out of 215 with no note of where 66 went, so they stay apart:

* **Which offer wins** (R-026) -- arithmetic. Price first, always. Commission only when
  the two prices are exactly equal, and a free commission outranks any number. There is
  no tolerance band anywhere in this file: the legacy comparator tolerated
  ``db_price * 1.10`` whenever commission improved, and it shipped three real raises of
  +3.0%, +2.0% and +2.9% on the run kept in the kept run.
* **Are these two things the same product** (R-024) -- decided by *the kind* of
  difference, never by a list of titles. The owner retired ``hard_property_titles`` on
  purpose: technical characteristics are an open domain, and a closed list makes every
  new characteristic soft by default, which is the expensive direction. So this file
  classifies each difference by what it *is* and routes it accordingly.
* **How many products come out** (R-021, R-031) -- an accounting identity, not a number.
  Every input product leaves with a disposition. If the arithmetic does not close, this
  file raises instead of writing a shorter list.

The default is separation. Merging two products that are actually different throws away
the one with the better price, and that price was the entire reason the run happened.
Splitting costs one extra row in a list the owner reads. When this file cannot tell, it
says so in the artifact instead of guessing.

What decides, and what asks
---------------------------

Four rules, evaluated in the only order that leaves each of them something to decide.

1. **An add-on announced on one side only** separates the products. In code, no model
   (R-025). This is the single place where an omission decides anything, and it is
   deliberate: R-024 says an omission never means "different", and R-025 then says an
   add-on difference *can* mean exactly that. The owner's word there is "pueden", and a
   pipeline needs "son" -- a solar panel on one listing and not the other is a different
   kit, not a markup.
2. **A categorical contradiction on a title both vendors declared** separates them. In
   code, no model (R-024). "Gasoil" against "gasolina": two mutually exclusive values of
   one slot, and there is no middle ground between them. Inclusion does not count as
   contradiction, which is what keeps "Full Luces LED" from contradicting "Luces LED".
3. **A numeric difference is a question for the model**, whatever the prices say. There is
   no number between 1250W and 1000W that means "close enough", because the question is
   domain and not arithmetic (R-024). Two values of the same unit *are* comparable by
   code -- a unit names a physical magnitude and a title does not -- and the comparison
   is never made across units that cannot be put on one scale. "50 millas" against "80 km"
   normalizes; a unit the engine cannot resolve goes to the model with both literals
   attached rather than being assumed comparable.
4. **With nothing explicit against them, the price decides whether it is worth asking.**
   Below the band they consolidate without a model; at or above it the model is asked
   whether it is resale. G-08's band is a confidence interval, never a classifier, and
   the corpus shows it lying in both directions: solar 6kW and 12kW are 29% apart and
   different, the same motorcycle at 3200 and 4200 is 31% apart and identical. What
   separates those two is rule 1 or rule 2, both of which run before the band is read.

Only rule 4 merges, and only below the band. The polarity is R-024's: the code's job is
to refuse, and a merge has to earn its way past that. The gate over the model is
one-directional -- a verdict can merge a pair the code left undecided, and cannot undo a
refusal the code made. That is what makes the conservative default safe to ship.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

if __package__:
    from . import whatsapp
else:
    # Invoked as a plain script, which is how the runner and the corpus check call it.
    # ``sys.path[0]`` is then this directory rather than its parent, so the package is
    # not importable until the parent is added.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from catalog import whatsapp

# R-031 stamps every artifact with a version so a rerun can tell "the engine changed" from
# "the input changed".
ENGINE_VERSION = "compare/1"

# The resale band. Origin: the legacy script declared ``PRICE_TIEBREAK_REL = 0.25``
# and never read it, so the number existed in the codebase and not in the behaviour.
PRICE_TIEBREAK_REL = 0.25

# Two name-similarity gates, because "not a candidate" and "safe to merge without asking"
# are different questions, and collapsing them is how a cheap matcher ends up doing a
# model's job.
#
# Both were re-derived from the measured score distribution of the six-chat corpus rather
# than inherited. Scoring is information-weighted (see ``DocumentFrequency``), and reading
# the real run's pairs by hand leaves one clean separation:
#
#     >= 0.559   the same product, every time: MARCA ONEBOT against ONEBOT, JMD against
#                MARCA JMD, Jimpeng Panther against Jimpeg panter, VEDCA N3 against VEDCA
#                2026, DTTZH against DTTZH F6
#     <= 0.497   a different product: JINPENG against JINPENG (Híbrido), IZUKI PREDATOR
#                MAX against MAOX, BATERÍA SACO against BATERÍA TOPMAQ
#
# The gap between them is (0.497, 0.559). ``MERGE_FLOOR = 0.55`` sits inside it, which is
# the only claim being made about it: on this corpus that one number separates the two
# populations. The legacy ``MATCH_THRESHOLD`` was 0.60 and is not used, because 0.60 threw
# away two pairs (DTTZH against DTTZH F6, CHALLENGER against IZUKI CHALLENGER) that are
# the same product and had to buy them back as questions for the model.
#
# The candidate gate is far lower, and lower on purpose: the same-product tail of this
# corpus runs down to 0.341 (TRICICLO MVP SPORT against Triciclos MVP 0km) and 0.355
# (TRICICLO OIM 2026 against Triciclos OIM de pasaje), while the noise below 0.30 is pairs
# that share nothing but a category word. A pair under this line is not discarded: the
# product is reported unmatched *with the score that put it there*, and the database
# confrontation that follows still sees it. Being wrong here defers a duplicate to a
# stage with more evidence; being wrong the other way sends the model work it cannot use.
MERGE_FLOOR = 0.55
CANDIDATE_FLOOR = 0.34

SAME = "SAME"
DIFFERENT = "DIFFERENT"
TRIAGE = "TRIAGE"

# Every value here is one a human can check against the evidence attached to the pair,
# which is the only thing that makes "conservative default" auditable rather than a
# slogan.
RULE_ADDON = "ADDON_ASYMMETRY"
RULE_CATEGORICAL = "CATEGORICAL_CONTRADICTION"
RULE_NUMERIC = "NUMERIC_SPEC_DIFFERENCE"
RULE_UNIT = "UNIT_UNRESOLVED"
RULE_MIXED_KIND = "MIXED_VALUE_KINDS"
RULE_NO_PRICE = "NO_PRICE_DECLARED"
RULE_BAND = "PRICE_BAND_AT_OR_ABOVE_BAND"
RULE_THIN = "THIN_MATCH_NO_EXPLICIT_DIFFERENCE"
RULE_CONSOLIDATE = "CONSOLIDATED_BELOW_BAND"

# One disposition per input product, always.
STANDALONE = "STANDALONE"
WINNER = "GROUP_WINNER"
CONSOLIDATED = "CONSOLIDATED"
DUPLICATE = "DUPLICATE_IN_SOURCE"

# The first two are the closed ``run_items.reason`` set from migration 0010 and are what
# a ``SKIPPED`` row carries. The third is step-2 bookkeeping that never reaches a row:
# a product that is merely unmatched has not left the run, it just has no partner.
REASON_CONSOLIDATED = "CONSOLIDATED_INTO_OTHER"
REASON_DUPLICATE = "DUPLICATE_IN_SOURCE"
REASON_NO_CANDIDATE = "NO_CROSS_PROVIDER_CANDIDATE"
REASON_NONE = ""

# The verdicts ``triage.py`` may return, and the only ones. A free-text verdict is a
# verdict nobody can gate.
TRIAGE_SAME = "same_product"
TRIAGE_DIFFERENT = "different_product"
TRIAGE_VERDICTS = (TRIAGE_SAME, TRIAGE_DIFFERENT)


class CompareRejected(Exception):
    """The engine will not emit a list whose arithmetic does not close."""


class FunnelUnreconciled(CompareRejected):
    """Input count and output dispositions disagree. The legacy bug, caught."""


# ───────────────────────────── text folding ─────────────────────────────

# A year is not an identity discriminator (R-024). "Moto Eléctrica Topmaq F3 2026" and
# "Moto Eléctrica Topmaq F3" are the same motorcycle; the owner's ruling was that the
# usual case is a dated model against an undated one. Only four-digit years are stripped,
# and only as standalone tokens -- a bare ``\d{4}`` would eat "6000" out of a 6kW solar
# panel and turn two different panels into one.
YEAR_TOKEN = re.compile(r"\b(?:19|20)\d{2}\b")

# Generic connective tissue. Deliberately smaller than the legacy STOP list, which also
# dropped "electrica", "electrico" and "marca": dropping the power-source words would
# make an electric and a combustion motorcycle of the same model collide, and dropping
# "marca" let the word "marca" carry a name on its own. Both are the expensive
# direction, so they stay in.
FILLER_TOKENS = frozenset(
    {
        "de", "del", "la", "el", "los", "las", "y", "e", "o", "u", "con", "para", "por",
        "un", "una", "unos", "unas", "en", "a", "al", "su", "sus", "lo", "the", "and", "s",
    }
)


def fold_text(text: str) -> str:
    """Lowercase, accent-free, symbol-free, single-spaced.

    Accents come off because "eléctrico" and "electrico" are one word written by two
    people; symbols come off because vendors put emoji inside product names. Digits
    survive, which is the whole reason this is not the legacy ``[^a-z0-9]`` fold wearing
    a different name -- a 6kW panel is not the same product as a 12kW one, and a fold
    that deleted both numbers would have nothing left to tell them apart.
    """
    decomposed = unicodedata.normalize("NFD", text.lower())
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    # Symbols, marks and controls go. Punctuation goes too, but as a **separator rather
    # than a deletion**: "TRICICLO UNO (CON TECHO)" folded to the token "techo)" and
    # "CON TECHO" to "(con", so the same name scored differently depending on whether a
    # vendor wrote brackets -- and on a two-record probe that pushed a real pair under the
    # candidate floor. Deleting instead of separating was no better: "DDS-OD" became
    # "ddsod", one token that matches nothing. The two decimal marks are the exception,
    # because "5.40" is five and a half and not five followed by a unit called forty.
    separator = "".join(
        " " if (unicodedata.category(ch)[0] in ("S", "P") and ch not in ".,") else ch
        for ch in stripped
    )
    stripped = "".join(
        ch for ch in separator if unicodedata.category(ch)[0] not in ("S", "M", "C")
    )
    return re.sub(r"\s+", " ", YEAR_TOKEN.sub(" ", stripped)).strip()


def fold_token(token: str) -> str:
    """One token of a folded name, plural-folded.

    Only regular plurals. Irregular pairs ("luz"/"luces") stay apart, deliberately: a
    missed fold can only cost an extra split, while a wrong fold could read two
    different values as the same one.
    """
    if len(token) > 3 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def name_tokens(text: str) -> tuple[str, ...]:
    """The tokens a name is compared on: folded, de-filled, plural-folded, sorted.

    Sorted rather than collected into a set, because this engine builds no unordered
    structure it later walks. CPython salts string hashes per process, and an order taken
    from one would make the candidate list -- and therefore the LLM's batches -- differ
    between two runs on the same corpus. Membership tests below use frozensets; nothing
    iterates one.
    """
    folded = [fold_token(token) for token in fold_text(text).split()]
    kept = {token for token in folded if token and token not in FILLER_TOKENS
            and len(token) > 1}
    return tuple(sorted(kept))


def ratio(left: str, right: str) -> float:
    """Character-level similarity. Deterministic for identical inputs, unlike a hash."""
    return SequenceMatcher(None, left, right).ratio()


# ────────────────────── commercial terms and add-ons ──────────────────────

# R-024 says a commercial term is detected "by phrase in the supplier's text", and R-027
# says it decides nothing and travels nowhere. This vocabulary is Spanish trade language,
# not this customer's data: no vendor name, no product, no catalogue id, no proper noun.
# That is deliberate -- the corpus ships "FACTURA DE MYPIME A NOMBRE DEL CLIENTE" on 17
# products, and the phrase "factura" already catches it. Naming the customer instead of
# the concept is exactly the leak R-019 level 4 exists to prevent.
#
# The failure direction is the point. A line missing from this table reads as a technical
# specification, so it can only ever produce an extra split or an extra trip to the model.
# It can never merge two products.
COMMERCIAL_PHRASES: tuple[str, ...] = (
    r"forma[s]? de pago", r"formas de pago", r"\bzelle\b", r"\bmn\b", r"\bmlc\b", r"\bcup\b",
    r"\busd\b", r"\beur\b", r"\beuros?\b", r"efectivo", r"transferencia", r"\bpago\b",
    r"\btransfer\b", r"tarjeta", r"\bbitcoin\b", r"contra entrega",
    r"transporte incluido", r"transpot[a-z]* incluido", r"entrega a domicilio",
    r"\bdelivery\b", r"\benv[ií]o\b", r"envio incluido", r"reparto", r"entrega gratis",
    r"\btransporte\b",
    r"mensajer[ií]a", r"mensajeria", r"con mensajer", r"\bmessenger\b",
    r"garant[ií]a", r"garantia", r"\bwarranty\b", r"garantizado",
    r"papeles", r"\bfactura\b", r"documentaci[oó]n", r"\bcomprobante\b",
    r"documentos", r"\bnit\b",
)

# Add-ons carry their own list because they do two things at once, and R-027-bis is the
# sentence that says so: their presence separates products -- which is what R-025 asks
# for -- and they enrich nobody, which is R-027. The pair of behaviours is not a
# contradiction; "separates, does not enrich" is one behaviour.
#
# Each entry is (canonical name, pattern). The canonical name is what the asymmetry test
# compares, so "techo", "con techo" and "techo con tondo" are one add-on and not three.
#
# Every pattern here requires an *announcement*, never a bare mention. The first draft
# matched the bare noun and it cost 169 false separations on this corpus: all 18 "goma"
# lines are tyre specs ("Gomas Rin 12 delanteras"), not an accessory being sold, and one
# vendor writing out its tyres while another did not is not a different product. A
# vocabulary of add-ons can only ever cost a split, so it is worth keeping short and
# precise -- but a wrong entry is not free, it is 169 wrong answers.
ADD_ON_PHRASES: tuple[tuple[str, str], ...] = (
    ("techo", r"\btechos?\b"),
    ("extensor de rango", r"\bextensore?s?\b|\bexpansor\s+de\s+rango\b"),
    ("cabina", r"\bcabinas?\b"),
    ("panel solar", r"\bpaneles?\s+solares?\b"),
    ("inversor", r"\binversores?\b"),
    ("goma", r"\bcon\s+gomas?\b"),
    ("parabrisas", r"\bcon\s+parabrisas?\b"),
)

_COMMERCIAL_RE = re.compile("|".join(COMMERCIAL_PHRASES), re.I)
_COMPILED_ADD_ONS = tuple((name, re.compile(pattern, re.I))
                          for name, pattern in ADD_ON_PHRASES)

# Section headers the vendors use to introduce their own sheet. They hold no value and
# two vendors spell them differently, so left in as axes they would read as a categorical
# contradiction on nothing.
SECTION_HEADERS = frozenset({
    "caracteristicas", "caracteristica", "especificaciones", "especificacion",
    "medidas de la cama", "medidas", "datos", "detalles", "descripcion",
    "tecnica", "ficha tecnica", "caracteristicas", "presentacion",
})

# Payment vocabulary, separated from the commercial phrases because it is the one place
# R-027 attaches an exception, and an exception needs its own test to be auditable.
PAYMENT_TOKEN = re.compile(r"\b(pago|pagos|zelle|mlc|cup|mn|usd|eur|euro|efectivo)\b", re.I)


def is_commercial(text: str) -> bool:
    """Does this line state one of the vendor's own commercial terms?"""
    return _COMMERCIAL_RE.search(fold_text(text)) is not None


def is_payment(text: str) -> bool:
    return PAYMENT_TOKEN.search(fold_text(text)) is not None


# "sin techo" is the *absence* of the add-on, and counting it as presence would invert
# the rule that uses it. The window is short on purpose: it stops at a clause boundary so
# that "sin techo, con cabina" only negates the techo.
_ADD_ON_NEGATION = re.compile(r"\bsin\b[^.;:•]{0,14}$", re.I)


def add_ons_in(text: str) -> tuple[str, ...]:
    """Which add-ons a product announces as included, sorted by canonical name.

    Read over the whole caption and not the name alone, because both halves happen in the
    corpus: "Triciclos Mvp 0km con Extensor de Rango" announces one in the name, and
    "Triciclo Power Red (con Techo)" announces it in a line below. Missing the second
    shape would silently drop half the add-on separations the corpus contains.
    """
    found: list[str] = []
    for canonical, pattern in _COMPILED_ADD_ONS:
        for match in pattern.finditer(text):
            prefix = text[max(0, match.start() - 16):match.start()]
            if _ADD_ON_NEGATION.search(prefix):
                continue
            found.append(canonical)
            break
    return tuple(sorted(set(found)))


# ───────────────────────────── units ─────────────────────────────

# A unit names a physical magnitude; a title does not. That asymmetry is the only reason
# an undeclared line can decide anything at all: "500 Wats" and "1500 Wats" are the same
# quantity measured twice, and no code has to know what a watt is to know that.
#
# These are definitions of units -- arithmetic inputs, not the app's ``units`` table.
# That table is a value catalogue the validator resolves at run time (R-020) and none of
# its codes are here; ``validate_units`` refuses a shape it cannot read, so a bad
# override fails loudly instead of quietly mis-normalizing a range.
UNIT_MAGNITUDES: dict[str, dict[str, float]] = {
    "length": {"m": 1.0, "km": 1000.0, "cm": 0.01, "mm": 0.001,
               "mi": 1609.344, "ft": 0.3048, "in": 0.0254},
    "mass": {"kg": 1.0, "g": 0.001, "lb": 0.45359237, "ton": 1000.0},
    "volume": {"l": 1.0, "ml": 0.001, "cc": 0.001, "gal": 3.785411784},
    "power": {"w": 1.0, "kw": 1000.0},
    "energy": {"wh": 1.0, "kwh": 1000.0},
    "voltage": {"v": 1.0},
    "current": {"a": 1.0},
    "charge": {"ah": 1.0},
    "speed": {"kmh": 1.0, "mph": 1.609344},
    "count": {"und": 1.0, "persona": 1.0, "plaza": 1.0, "asiento": 1.0, "pasajero": 1.0},
    "time": {"s": 1.0, "min": 60.0, "h": 3600.0, "dia": 86400.0, "mes": 2592000.0},
}

# Spelling folding only. The corpus writes "wats", "watt", "watts" and "w" for one unit
# and "kilowats" for a thousand of them; nothing here says which numbers are compatible,
# only which spellings name the same unit.
UNIT_ALIASES: dict[str, str] = {
    "wat": "w", "watts": "w", "wats": "w", "vatio": "w", "watios": "w",
    "kilowat": "kw", "kilowats": "kw", "kilowatio": "kw",
    "volt": "v", "voltio": "v", "volts": "v", "voltsio": "v",
    "ampere": "a", "amper": "a", "amps": "a", "amperes": "a", "amp": "a", "amperaje": "a",
    "kilowatt-hora": "kwh", "kwh": "kwh",
    "klm": "km", "kilometro": "km", "kilometros": "km", "kilometers": "km", "kms": "km",
    "km/h": "kmh", "kph": "kmh", "km/hora": "kmh", "kilometros por hora": "kmh",
    "milla": "mi", "millas": "mi", "mile": "mi", "miles": "mi",
    "kilogramo": "kg", "kilo": "kg", "kilos": "kg", "kilogramos": "kg",
    "libra": "lb", "libras": "lb", "pounds": "lb",
    "litro": "l", "litros": "l", "lt": "l",
    "mililitro": "ml", "mililitros": "ml",
    "metro": "m", "metros": "m",
    "centimetro": "cm", "centimetros": "cm",
    "pie": "ft", "pies": "ft", "pulgada": "in", "pulgadas": "in",
    "tonelada": "ton", "toneladas": "ton",
    "unidad": "und", "unidades": "und",
    "personas": "persona", "pasajeros": "pasajero", "asientos": "asiento",
    "plazas": "plaza", "dia": "dia", "dias": "dia",
    "horas": "h", "hora": "h", "minutos": "min", "segundos": "s",
}

# A number as these vendors write it: "1250", "5.40", "54.000", "1,10", "3000".
#
# The thousands alternative **requires** at least one separator group. That is the whole
# point of writing it this way, and the first draft got it wrong by making the separator
# group optional -- so the alternation matched the first three digits of every plain
# four-digit number and left the rest behind: "1250 W" parsed as 125 W, "6000" as 600.
# Nothing crashed, the funnel still reconciled, and every comparison above 999 was
# comparing truncated numbers. A green check on a wrong number is worse than a failure.
_NUMBER = r"(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_UNIT_TOKEN = r"([A-Za-z°µ/]{1,18})"
# A range is one claim about a span, not two claims: "60-70 KM" is parsed as a pair and
# compared as a pair, so "60-70" against "70" is a difference and not a silent match on
# the second number.
#
# Number-first and unit-first are two patterns, tried in that order, rather than one
# alternation. In a single pattern the leftmost match wins regardless of which
# alternative it came from, so on "Autonomia de 60-70 KM" the unit-first branch matched
# the word "de" against the 60 and the range never got parsed at all. Trying the
# number-first pattern across the whole line before the other one is the only ordering
# that means what it says.
_QUANTITY_RE = re.compile(
    rf"{_NUMBER}\s*(?:-|a|to)\s*{_NUMBER}\s*{_UNIT_TOKEN}?"
    rf"|{_NUMBER}\s*{_UNIT_TOKEN}?"
)
# "$1250", "18\"" and the like: the unit is written before the number.
_QUANTITY_REVERSED_RE = re.compile(rf"{_UNIT_TOKEN}\s*{_NUMBER}")


def validate_units(table) -> None:
    """Refuse a unit table this file cannot use, by name and by value.

    A factor of zero or a string where a number belongs would not crash -- it would
    quietly normalize every range to zero and report that every pair agrees. Failing on
    the shape is cheaper than discovering that from a merge nobody questioned.
    """
    if not isinstance(table, dict) or not table:
        raise CompareRejected("unit table must be a non-empty object of dimensions")
    for dimension in sorted(table):
        factors = table[dimension]
        if not isinstance(factors, dict) or not factors:
            raise CompareRejected(f"dimension {dimension!r} carries no units")
        for unit in sorted(factors):
            factor = factors[unit]
            if not isinstance(factor, (int, float)) or isinstance(factor, bool):
                raise CompareRejected(
                    f"unit {unit!r} in {dimension!r} has factor {factor!r}, not a number"
                )
            if factor <= 0:
                raise CompareRejected(f"unit {unit!r} in {dimension!r} has factor {factor}")


def canonical_unit(raw: str) -> str:
    """Fold a written unit to its canonical spelling, or hand it back unchanged."""
    folded = re.sub(r"\s+", " ", raw.strip().lower().rstrip("."))
    return UNIT_ALIASES.get(folded, folded)


def unit_dimension(unit: str) -> str | None:
    """Which magnitude a canonical unit measures, or ``None`` when it measures none."""
    for dimension in sorted(UNIT_MAGNITUDES):
        if unit in UNIT_MAGNITUDES[dimension]:
            return dimension
    return None


def _to_number(raw: str) -> float:
    """Read a quantity the way these vendors write amounts.

    ``54.000`` and ``54,000`` are thousands separators; only a separator followed by one or
    two trailing digits is a decimal point. Getting it backwards turns a $5,400 scooter
    into a $5.40 one, which is the mistake this file exists to not make twice.
    """
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        return float(raw.replace(".", "").replace(",", ""))
    return float(raw.replace(",", "."))


@dataclass(frozen=True)
class Quantity:
    """A measured value: the numbers, the unit as written, the unit canonicalized."""

    numbers: tuple[float, ...]
    unit_raw: str
    unit: str
    dimension: str | None

    @property
    def is_range(self) -> bool:
        return len(self.numbers) > 1

    @property
    def normal(self) -> tuple[float, ...] | None:
        """The same value in the dimension's reference unit, or ``None`` if unknown."""
        if self.dimension is None:
            return None
        factor = UNIT_MAGNITUDES.get(self.dimension, {}).get(self.unit)
        if factor is None:
            return None
        return tuple(number * factor for number in self.numbers)


def parse_quantity(text: str) -> Quantity | None:
    """Read the first measurement in a line, or ``None`` when it holds no number."""
    cleaned = re.sub(r"[•·*_~#]+", " ", text)
    folded = fold_text(cleaned)
    match = _QUANTITY_RE.search(folded) or _QUANTITY_REVERSED_RE.search(folded)
    if match is None:
        return None
    numbers: list[float] = []
    unit_raw = ""
    for group in match.groups():
        if group is None:
            continue
        if re.fullmatch(_NUMBER, group):
            numbers.append(_to_number(group))
        else:
            unit_raw = group
    if not numbers:
        return None
    unit = canonical_unit(unit_raw)
    return Quantity(numbers=tuple(numbers), unit_raw=unit_raw.strip(), unit=unit,
                    dimension=unit_dimension(unit))


def _close(left: float, right: float) -> bool:
    """Float equality with a relative epsilon, so 0.1 + 0.2 == 0.3 without a threshold.

    The epsilon is a rounding tolerance on a subtraction, not a price tolerance, and it
    has nothing to do with ``PRICE_TIEBREAK_REL``. Both operands are read from digits
    that a human typed, so the only error available is decimal representation.
    """
    return abs(left - right) <= max(1e-9, 1e-9 * max(abs(left), abs(right)))


def quantities_agree(left: Quantity, right: Quantity) -> bool | None:
    """Do two measurements say the same thing?

    ``True``/``False`` when they can be put on one scale, ``None`` when they cannot -- and
    ``None`` is not permission to assume: it routes the pair to the model with both
    literals attached, which is what the owner asked for on the one unit mismatch in the
    corpus (50 miles against 80 km: comparable, and the same product).

    Three cases, in order. **Same unit** -- including a unit neither side recognizes:
    "2 juegos de llaves" against "3 juegos de llaves" is a claim about the same count
    whatever a juego is, and the factor cancels, so the numbers compare directly.
    **Different units of one magnitude** -- the conversion factor does the work, which is
    what "normalize for comparing, never for storing" means in practice. **Units that
    cannot be related** -- no answer from code.

    **What the conversion does not settle, and why that is not a bug.** 50 miles is 80.467
    km, so against a written "80 km" the two normalized values differ by 0.6% and this
    returns ``False``. That is the owner's example of two numbers that *are* the same
    product, and it does come out as one -- by asking, which is where R-024 puts a numeric
    difference. A rounding tolerance would collapse the two questions and the price band
    with them: at 1% every 6kW against a 6.1kW panel is "the same measurement", and a
    tolerance on a *specification* is precisely the threshold R-024 forbids ("no hay
    porcentaje que valva entre dominios"). The conversion is here to make the comparison
    possible and to tell the model that both numbers name one magnitude; the decision on
    whether 0.6% is a rounding artefact or a real difference is the domain question the
    owner assigned to the model. It is a question, not a merge, and this returns ``False``
    precisely so the question survives.

    The stored value never sees any of this: ``Quantity`` carries the numbers as written
    alongside the canonical unit, and nothing here writes back to the record.
    """
    if left.is_range != right.is_range:
        # "60-70 km" against "70 km" is not an equality test; the model gets to say
        # whether the span covers the point.
        return None
    if left.unit == right.unit:
        return all(_close(a, b) for a, b in zip(left.numbers, right.numbers))
    if left.normal is None or right.normal is None:
        return None
    if left.dimension != right.dimension:
        return None
    return all(_close(a, b) for a, b in zip(left.normal, right.normal))


# ───────────────────────────── axes ─────────────────────────────


@dataclass(frozen=True)
class Axis:
    """One declared property of a product.

    ``key`` is what makes two axes comparable. A declared title yields
    ``title:<folded label>``; an undeclared one yields ``unit:<canonical unit>`` when
    that unit is unambiguous inside this product, and yields nothing when it is not --
    "20 Amp" beside "45 Amp" is two batteries, not one axis, and an unanchored line is
    evidence for the model rather than a comparison by code.
    """

    key: str
    label: str
    value: str
    kind: str            # numeric | categorical | commercial | add_on
    quantity: Quantity | None
    raw: str
    scope: str           # generic | provider_scoped
    role: str            # compares | never_decides | separates
    source_provider: str

    def as_evidence(self) -> dict:
        """The shape the triage prompt sees. Both literals, never a recommendation."""
        return {
            "text": self.raw,
            "axis": self.key or None,
            "declared_title": self.label or None,
            "value": self.value,
            "kind": self.kind,
            "unit": self.quantity.unit_raw if self.quantity else None,
        }


def _is_section_header(text: str) -> bool:
    return fold_text(text).strip(" :•-") in SECTION_HEADERS


def _split_label(text: str) -> tuple[str, str]:
    """A caption line's title and value, when it declares one.

    The colon is the only separator that works across these six vendors, and it is read
    before any other fold because "Autonomía de 60-70 KM" contains none while
    "GARANTÍA: 15 DÍAS" does. A head longer than 60 characters is not a title, it is a
    sentence with a colon in it.
    """
    stripped = re.sub(r"^[•·\-–—\s]+", "", text).strip()
    head, sep, tail = stripped.partition(":")
    if not sep:
        return "", stripped
    label = fold_text(head).strip()
    if not label or len(label) > 60 or _is_section_header(head):
        return "", stripped
    value = re.sub(r"^[•·\-–—\s]+", "", tail).strip()
    return label, value


def _ambiguous_units(quantities: list[Quantity]) -> frozenset[str]:
    """Canonical units this caption declares with two different values.

    Such a unit does not name one quantity here, so it is refused as an anchor and its
    lines travel to the model as evidence instead.
    """
    seen: dict[str, tuple[float, ...]] = {}
    ambiguous: set[str] = set()
    for quantity in quantities:
        if quantity.dimension is None or quantity.normal is None:
            continue
        if quantity.unit not in seen:
            seen[quantity.unit] = quantity.numbers
        elif seen[quantity.unit] != quantity.numbers:
            ambiguous.add(quantity.unit)
    return frozenset(ambiguous)


@dataclass
class ProductView:
    """One input product, read the way this engine compares on."""

    item_key: str
    provider_key: str
    ordinal: int
    name: str
    normalized_name: str
    tokens: tuple[str, ...]
    price: float | None
    commission: dict | None
    axes: tuple[Axis, ...]
    unanchored: tuple[str, ...]
    add_ons: tuple[str, ...]
    duplicate_of: str | None = None

    @property
    def fingerprint(self) -> tuple:
        """What makes two offers from one vendor literally the same announcement."""
        return (self.provider_key, self.normalized_name, self.price)

    @property
    def comparable_axes(self) -> tuple[Axis, ...]:
        return tuple(axis for axis in self.axes if axis.role == "compares")


def announced_scope(record: dict) -> str:
    """The text that describes *this* variant, and nothing else.

    Step 1 expands one multi-price caption into one record per price, and each record's
    ``prices[]`` entry carries the variant context that caption announced it with --
    ``con extensor de rango``, ``sin techo``, ``(híbrido)``. That context is the only
    per-variant signal in the record, and it is the one this engine needs.

    The whole caption is used **only when the caption produced a single offer**. A
    caption that announced two prices describes two products, and reading both add-ons
    into both records is how "TRICICLO VIENTO RÁPIDO" and "TRICICLO VIENTO RAPIDO con
    Extensor de Rango" came out as the same product: the caption mentioned the extensor
    once, so both records claimed it, so there was no asymmetry to find. The live triage
    run merged those two; this is the reason it was able to.
    """
    prices = record.get("prices") or []
    if len(prices) <= 1:
        return (record.get("name") or "") + "\n" + (record.get("text") or "")
    context = ""
    for price in prices:
        if price.get("value") == record.get("price"):
            context = price.get("context") or ""
            break
    return (record.get("name") or "") + "\n" + context


def build_view(record: dict) -> ProductView:
    """Read one ingested record into the shape this engine compares on.

    The product name is dropped out of the spec lines when it appears there, because step
    1 keeps every caption line and the first one usually *is* the name. Left in, it would
    sit on its own axis where it differs from the other vendor's name by construction and
    refuse every merge for free.
    """
    provider_key = record["provider_key"]
    name = record.get("name") or ""
    normalized_name = fold_text(name)

    lines: list[tuple[str, str]] = []
    for entry in record.get("spec_lines") or []:
        raw = entry.get("text") or ""
        if not raw.strip() or fold_text(raw) == normalized_name:
            continue
        if _is_section_header(raw):
            continue
        lines.append((raw, entry.get("source_provider") or provider_key))

    parsed: list[tuple[str, str, str, str, Quantity | None]] = []
    for raw, owner in lines:
        label, value = _split_label(raw)
        quantity = parse_quantity(value if label else raw)
        parsed.append((raw, owner, label, value, quantity))

    ambiguous = _ambiguous_units([q for _, _, _, _, q in parsed if q is not None])

    axes: list[Axis] = []
    unanchored: list[str] = []
    for raw, owner, label, value, quantity in parsed:
        if is_commercial(raw):
            # R-024: a commercial term decides identity never. R-027: it travels never.
            # It becomes no comparable axis, and keeps only its role on the winner.
            axes.append(Axis("", label, fold_text(raw), "commercial", None, raw,
                             "provider_scoped", "never_decides", owner))
            continue
        if any(name in add_ons_in(raw) for name, _ in _COMPILED_ADD_ONS):
            # R-027-bis: an add-on separates products and enriches nobody. Presence is
            # what matters, so it carries no comparable axis either.
            axes.append(Axis("", label, fold_text(raw), "add_on", None, raw,
                             "provider_scoped", "separates", owner))
            continue

        if label:
            axes.append(Axis(
                f"title:{fold_token(label)}", label, fold_text(value),
                "numeric" if quantity else "categorical", quantity, raw,
                "generic", "compares", owner,
            ))
            continue

        if quantity is not None and quantity.dimension is not None \
                and quantity.unit not in ambiguous:
            axes.append(Axis(
                f"unit:{quantity.unit}", "", fold_text(raw), "numeric", quantity, raw,
                "generic", "compares", owner,
            ))
            continue

        # No declared title, or a number whose unit is unrecognized or ambiguous inside
        # this caption. Either way the line has no anchor, so it is evidence and nothing
        # more -- it reaches the model and it decides nothing by code.
        unanchored.append(raw)

    return ProductView(
        item_key=record["item_key"],
        provider_key=provider_key,
        ordinal=int(record.get("ordinal") or 0),
        name=name,
        normalized_name=normalized_name,
        tokens=name_tokens(name),
        price=record.get("price"),
        commission=record.get("commission"),
        axes=tuple(axes),
        unanchored=tuple(unanchored),
        add_ons=add_ons_in(announced_scope(record)),
    )


# ───────────────────────── differences ─────────────────────────


@dataclass(frozen=True)
class Difference:
    """One disagreement between two products, labelled by what kind of thing it is."""

    axis: str
    kind: str          # addon | categorical | numeric | unit | mixed
    left: str
    right: str
    note: str

    def as_dict(self) -> dict:
        return {"axis": self.axis, "kind": self.kind, "left": self.left,
                "right": self.right, "note": self.note}


def contradicts(left: str, right: str) -> bool:
    """Do two categorical values contradict each other?

    Strictly disjoint token sets with no inclusion either way. Inclusion is what keeps
    "Full Luces LED" from contradicting "Luces LED" -- one is a superset, so nobody is
    claiming the other is wrong. Disjointness is what makes "gasoil" and "gasolina" a
    contradiction, the owner's example and the reason this branch exists in code at all.
    """
    left_tokens = frozenset(name_tokens(left))
    right_tokens = frozenset(name_tokens(right))
    if not left_tokens or not right_tokens:
        return False
    if left_tokens <= right_tokens or right_tokens <= left_tokens:
        return False
    return not (left_tokens & right_tokens)


def compare_axes(left: ProductView, right: ProductView) -> tuple[Difference, ...]:
    """Every disagreement the two products state explicitly, typed.

    The comparison runs over the **intersection** of what both sides declared, which is
    R-024's criterion written as an algorithm: a property one vendor omitted is not a
    difference, and a union would turn every omission into one.
    """
    right_by_key: dict[str, Axis] = {}
    for axis in right.comparable_axes:
        # First wins, so a vendor that writes the same title twice does not make the
        # outcome depend on which copy was read last.
        right_by_key.setdefault(axis.key, axis)

    differences: list[Difference] = []
    for axis in left.comparable_axes:
        other = right_by_key.get(axis.key)
        if other is None:
            continue
        if axis.value == other.value:
            # The same words on both sides are not a difference. This line earns its
            # place: without it, "🎨COLOR: 🔵" against "🎨COLOR: 🔴" compares as two empty
            # strings, and two lines that both read "2 juegos de llaves" take a trip to
            # the model to be told they are the same sentence.
            continue
        if axis.kind == "numeric" and other.kind == "numeric":
            agree = quantities_agree(axis.quantity, other.quantity)
            if agree is True:
                continue
            if agree is None:
                differences.append(Difference(
                    axis.key, "unit", axis.raw, other.raw,
                    "both numbers exist but not on one scale"))
            else:
                differences.append(Difference(
                    axis.key, "numeric", axis.value, other.value,
                    "same magnitude, different number; no threshold applies"))
            continue
        if axis.kind == "categorical" and other.kind == "categorical":
            if axis.value != other.value and contradicts(axis.value, other.value):
                differences.append(Difference(
                    axis.key, "categorical", axis.value, other.value,
                    "two mutually exclusive values of one declared title"))
            continue
        # One side declared a measurement, the other a word. That cannot be read either
        # way by code, so it is a question and not a refusal.
        differences.append(Difference(
            axis.key, "mixed", axis.value, other.value,
            "one side declared a measurement, the other a word"))
    return tuple(differences)


def add_on_difference(left: ProductView, right: ProductView) -> Difference | None:
    """R-025's one deterministic separation: an add-on on one side only."""
    only_left = [name for name in left.add_ons if name not in right.add_ons]
    only_right = [name for name in right.add_ons if name not in left.add_ons]
    if not only_left and not only_right:
        return None
    return Difference(
        "add_on", "addon",
        ", ".join(only_left) or "(none)", ", ".join(only_right) or "(none)",
        "an add-on is announced on one side only",
    )


# ───────────────────────── candidate scoring ─────────────────────────


def _align_tokens(left: tuple[str, ...], right: tuple[str, ...]) -> list[tuple[str, str]]:
    """Pair the tokens of two names up: exactly where possible, fuzzily where close.

    Exact first, because it is both cheaper and more reliable than a character ratio.
    The fuzzy pass then only looks at what is left, so "yoazaki" cannot be paired with
    "yoazaky" while the exact "707" sitting next to them is still available to be paired
    with a different number. That ordering is the whole reason this returns a pairing and
    not a score.

    The corpus needs the fuzzy pass: it writes "Yoazaki"/"Yoazaky" and "Jimpeng"/"Jimpeg"
    for the same two models, and a candidate generator that misses those never gets the
    chance to notice they are the same product. The length gate keeps the inner loop from
    comparing everything to everything.
    """
    remaining = list(range(len(right)))
    matched: list[tuple[str, str]] = []
    matched_left: list[str] = []

    for token in left:
        for index in remaining:
            if right[index] == token:
                remaining.remove(index)
                matched.append((token, token))
                matched_left.append(token)
                break

    for token in left:
        if token in matched_left:
            continue
        best_index = -1
        best_ratio = 0.0
        for index in remaining:
            if abs(len(right[index]) - len(token)) > 2:
                continue
            value = ratio(token, right[index])
            if value > best_ratio:
                best_index, best_ratio = index, value
        if best_index >= 0 and best_ratio >= 0.84:
            remaining.remove(best_index)
            matched.append((token, right[best_index]))
            matched_left.append(token)
    return matched


class DocumentFrequency:
    """How many of this run's products mention each name token, and what that is worth.

    The weighting is inverse document frequency, computed from the run itself, and it is
    the fix for the failure a plain token overlap cannot avoid: every product in this
    corpus is called "BICICLETA ELÉCTRICA SOMETHING", so "bicicleta" and "electrica"
    match on every pair and drown the model name that actually distinguishes them. The
    first draft of this file scored on raw overlap and merged seven different electric
    bicycles into one group of seventeen -- the category prefix carried it, and nothing
    in the score noticed that DTTZH and VIPORVIVE are different bicycles.

    A token that appears on forty of two hundred and fifteen products says almost nothing
    about whether two products are the same; a token that appears on two says almost
    everything. That is arithmetic over the run's own data, so it improves as the catalog
    does and needs no list of category words -- which matters, because this customer's
    subcategory names are exactly the kind of hardcoded vocabulary R-019 level 4 forbids
    in the runner, and the legacy matcher leaned on them (``db_tokens`` added the DB
    subcategory to every row) to get this same effect.
    """

    def __init__(self, views: tuple[ProductView, ...] | list[ProductView]):
        counts: dict[str, int] = {}
        for view in views:
            for token in view.tokens:
                counts[token] = counts.get(token, 0) + 1
        self.total = len(views)
        self.counts = counts

    def idf(self, token: str) -> float:
        """How much a shared token is worth. Smoothed, and never zero.

        The ``log(1 + N/df)`` form keeps a token that appears on every product at a small
        positive weight instead of dropping it to infinity's problem, and keeps a token
        unique to one product from dwarfing every other signal in the sum.
        """
        import math

        frequency = self.counts.get(token, 0)
        if frequency <= 0:
            frequency = 1
        return math.log(1.0 + self.total / frequency)

    def weight(self, tokens) -> float:
        """The total weight of a token collection. Membership tests only, never iterated."""
        return sum(self.idf(token) for token in tokens)


def name_score(left: ProductView, right: ProductView,
               frequency: DocumentFrequency | None = None) -> float:
    """How likely two names are to be the same product, in [0, 1].

    Information-weighted overlap carries the score: the sum of the weights of the tokens
    the two names share, over the sum of the weights of everything either name has. A
    character ratio is admitted only when neither side carries an unshared token of real
    weight -- otherwise "Triciclo Jinpeng" scores well against "Triciclo Jinpeng 3500" for
    the wrong reason, and "Moto Eléctrica X" against "Moto X" for a right one the engine
    should not decide on a name.
    """
    if not left.tokens or not right.tokens:
        return 0.0
    frequency = frequency or DocumentFrequency([left, right])

    aligned = _align_tokens(left.tokens, right.tokens)
    matched_left = [token for token, _ in aligned]
    matched_right = [token for _, token in aligned]
    only_left = [token for token in left.tokens if token not in matched_left]
    only_right = [token for token in right.tokens if token not in matched_right]

    shared = frequency.weight(sorted(matched_left))
    union = shared + frequency.weight(sorted(only_left)) + frequency.weight(sorted(only_right))
    if union <= 0:
        return 0.0
    score = shared / union

    if not only_left and not only_right:
        # Nothing unshared on either side, so the two names differ only in spelling and
        # spacing. A character ratio is allowed to raise the score there and nowhere
        # else: admitting it against "MOTO ELECTRICA VEDCA" versus "MOTO ELECTRICA VEDCA
        # N3" is how a matcher decides on a name it has no business deciding on.
        score = max(score, ratio(left.normalized_name, right.normalized_name) * 0.75)
    return round(min(1.0, score), 6)


# ───────────────────────── the comparator ─────────────────────────


def commission_score(commission: dict | None) -> float:
    """How good a commission is for the owner, higher is better (R-026).

    Three cases and they are three, not two:

    * **free** -- the owner sets their own margin. Best there is: ``+inf``.
    * **a value** -- the value.
    * **not declared** -- unknown, which is the worst case and not the best: ``-inf``.

    The legacy comparator folded "free" and "not declared" into one ``None`` and ranked
    that ``None`` first, so an offer that said nothing about commission beat one paying
    50 on an exact price tie. The port of that function inherited the fold. Saying nothing
    is not an offer of anything, and it must not win a tie against a stated number.

    Consulted **only** on an exact price tie. A commission never buys a worse price.
    """
    if not commission:
        return float("-inf")
    if commission.get("free"):
        return float("inf")
    value = commission.get("value")
    if value is None:
        return float("-inf")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf")


def commission_of(product: ProductView) -> float | None:
    """The stated commission value, ``None`` when it is free or was never declared."""
    commission = product.commission
    if not commission or commission.get("free"):
        return None
    value = commission.get("value")
    return None if value is None else float(value)


def winner_key(product: ProductView) -> tuple:
    """R-026's total order: price, then commission, then identity.

    No tolerance multiplies any component. That single absence is the whole difference
    from the legacy comparator, which let a price ten percent higher through whenever the
    commission improved and raised real products because of it.
    """
    price = product.price
    return (
        float("inf") if price is None else float(price),
        -commission_score(product.commission),
        product.item_key,
    )


def price_gap(left: ProductView, right: ProductView) -> float | None:
    """Relative price gap, or ``None`` when one side declared no price.

    The base is the **cheaper** of the two, and that is not a free choice -- it is the
    arithmetic G-08's own worked examples are written in. The requirement calls solar 6kW at
    4500 against 12kW at 5800 a "29%" difference, and the same motorcycle at 3200 against
    4200 a "31%": both are ``(high - low) / low``. Taking the expensive side as the base
    instead reports the same two pairs as 22.4% and 23.8%, which would drop both below the
    25% line and merge on price a case the owner explicitly put above it.

    The first draft of this file used the expensive side as the base, on the reasoning that
    it stops a 100-against-400 pair from reading as 300%. That reasoning is sound as
    arithmetic and wrong as a reading of the requirement: a 4x gap really is a 300% markup,
    and the band's job is to decide whether that is far enough to be worth asking about
    rather than to compress the number until it looks comfortable.

    Measured on the six-chat corpus, five candidate pairs sit on the wrong side of the line
    because of it (37 above the band against 32). One of the five changes the *reason* it is
    asked about -- 25.0% against 20.0%, and it now reports PRICE_BAND_AT_OR_ABOVE_BAND
    instead of a thin name match -- and the other four are already routed elsewhere by an
    earlier rule, so no product moves. The bug was small on this corpus and wrong in
    principle: it moves in the direction of merging on price, which is the expensive side of
    the default. It is corrected here rather than left because the corpus would not have
    caught it twice.
    """
    if left.price is None or right.price is None:
        return None
    high = max(float(left.price), float(right.price))
    low = min(float(left.price), float(right.price))
    if low <= 0:
        return None
    return round((high - low) / low, 6)


def pair_id(left: str, right: str) -> str:
    """A stable name for a pair, whichever side the loop reached first."""
    low, high = sorted([left, right])
    return f"{low}__{high}"


@dataclass(frozen=True)
class PairDecision:
    """What the engine concluded about one pair, and what it would take to disagree."""

    pair_id: str
    left: str
    right: str
    score: float
    verdict: str
    rule: str
    price_gap: float | None
    differences: tuple[Difference, ...]
    asked: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "pair_id": self.pair_id, "left": self.left, "right": self.right,
            "score": self.score, "verdict": self.verdict, "rule": self.rule,
            "price_gap": self.price_gap, "asked": list(self.asked),
            "differences": [difference.as_dict() for difference in self.differences],
        }


def decide_pair(left: ProductView, right: ProductView, score: float) -> PairDecision:
    """The four rules of the module docstring, in order."""
    identifier = pair_id(left.item_key, right.item_key)
    gap = price_gap(left, right)

    asymmetry = add_on_difference(left, right)
    if asymmetry is not None:
        return PairDecision(identifier, left.item_key, right.item_key, score, DIFFERENT,
                            RULE_ADDON, gap, (asymmetry,))

    differences = compare_axes(left, right)
    if any(difference.kind == "categorical" for difference in differences):
        return PairDecision(identifier, left.item_key, right.item_key, score, DIFFERENT,
                            RULE_CATEGORICAL, gap, differences)

    asked = tuple(sorted({difference.kind for difference in differences}))
    if asked:
        if "unit" in asked:
            rule = RULE_UNIT
        elif "mixed" in asked:
            rule = RULE_MIXED_KIND
        else:
            rule = RULE_NUMERIC
        return PairDecision(identifier, left.item_key, right.item_key, score, TRIAGE,
                            rule, gap, differences, asked)

    if gap is None:
        # No price on one side: no band to read and no evidence to merge on. Separated,
        # and said so, rather than assumed to be a resale of the same thing.
        return PairDecision(identifier, left.item_key, right.item_key, score, DIFFERENT,
                            RULE_NO_PRICE, gap, differences, ("no_price",))

    if gap >= PRICE_TIEBREAK_REL:
        return PairDecision(identifier, left.item_key, right.item_key, score, TRIAGE,
                            RULE_BAND, gap, differences, ("price_band",))

    if score < MERGE_FLOOR:
        # Nothing explicit against them, prices close, names half a match. The only
        # evidence the merge would rest on is the name, which is a question for the model.
        return PairDecision(identifier, left.item_key, right.item_key, score, TRIAGE,
                            RULE_THIN, gap, differences, ("thin_match",))

    return PairDecision(identifier, left.item_key, right.item_key, score, SAME,
                        RULE_CONSOLIDATE, gap, differences)


# ───────────────────────── triage evidence ─────────────────────────


def triage_evidence(left: ProductView, right: ProductView,
                    decision: PairDecision) -> dict:
    """Everything the model is allowed to see about one pair.

    Both products' own words, both sides of every difference, the price band. What it
    does not get is a recommendation, a rule name to pattern-match on, or one vendor's
    text without the other's -- an evidence set with three offers in it and one product
    in it has an answer before anybody reads it.
    """
    def side(product: ProductView) -> dict:
        return {
            "item_key": product.item_key,
            "name": product.name,
            "price": product.price,
            "commission": commission_of(product),
            "declared": [axis.as_evidence() for axis in product.comparable_axes],
            "undeclared": list(product.unanchored),
            "add_ons": list(product.add_ons),
        }

    return {
        "pair_id": decision.pair_id,
        "left": side(left),
        "right": side(right),
        "name_similarity": decision.score,
        "price_gap": decision.price_gap,
        "price_band_rel": PRICE_TIEBREAK_REL,
        "questions": list(decision.asked),
        "stated_differences": [difference.as_dict() for difference in decision.differences],
    }


# ───────────────────────── grouping ─────────────────────────


class _Union:
    """Union-find over item keys with a deterministic shape.

    Written out rather than imported because the grouping rule matters as much as the
    structure: a chain A~B, B~C where A and C contradict each other must not silently
    become one product. Edges are therefore offered in a fixed order and each is checked
    against every pair it would join before it is accepted.
    """

    def __init__(self, keys: tuple[str, ...]):
        self.parent = {key: key for key in keys}
        self.keys = keys

    def find(self, key: str) -> str:
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != root:
            self.parent[key], key = root, self.parent[key]
        return root

    def members(self, root: str) -> list[str]:
        return sorted(key for key in self.keys if self.find(key) == root)

    def union(self, left: str, right: str) -> None:
        low, high = sorted([self.find(left), self.find(right)])
        if low != high:
            self.parent[high] = low


@dataclass
class Group:
    """One product after consolidation: a winner and what it absorbed."""

    group_id: str
    members: list[str]
    winner: str
    basis: str
    notes: tuple[str, ...] = ()


def _merge_edges(views_by_key: dict[str, ProductView],
                 decisions: tuple[PairDecision, ...],
                 triage_verdicts: dict[str, dict],
                 frequency: DocumentFrequency) -> tuple[list[tuple], dict]:
    """Turn SAME decisions into merge edges, refusing any that would contradict.

    Transitivity is not a gift. A~B and B~C do not give A~C when A and C contradict each
    other on a declared title, and a group holding such a pair has merged exactly what
    the engine spent the stage keeping apart. Every edge is checked against every pair it
    would join; the ones that fail are reported instead of applied.
    """
    cache: dict[str, PairDecision] = {}

    def decide(left: str, right: str) -> PairDecision:
        identifier = pair_id(left, right)
        if identifier not in cache:
            cache[identifier] = decide_pair(
                views_by_key[left], views_by_key[right],
                name_score(views_by_key[left], views_by_key[right], frequency))
        return cache[identifier]

    ordered = sorted(
        (decision for decision in decisions
         if decision.verdict == SAME
         or (decision.verdict == TRIAGE
             and triage_verdicts.get(decision.pair_id, {}).get("verdict") == TRIAGE_SAME)),
        key=lambda decision: (-decision.score, decision.pair_id),
    )

    union = _Union(tuple(sorted(views_by_key)))
    edges: list[tuple[str, str, str]] = []
    refused: dict[str, dict] = {}

    for decision in ordered:
        left_members = union.members(union.find(decision.left))
        right_members = union.members(union.find(decision.right))
        if left_members == right_members:
            continue
        conflict = None
        for left in left_members:
            for right in right_members:
                inner = decide(left, right)
                if inner.verdict == DIFFERENT:
                    conflict = {
                        "edge": decision.pair_id,
                        "refused_pair": [left, right],
                        "rule": inner.rule,
                        "differences": [d.as_dict() for d in inner.differences],
                    }
                    break
            if conflict:
                break
        if conflict:
            refused[decision.pair_id] = conflict
            continue
        union.union(decision.left, decision.right)
        edges.append((decision.left, decision.right, decision.rule))
    return edges, {"refused": refused, "applied": len(edges)}


def _build_groups(views: list[ProductView], decisions: tuple[PairDecision, ...],
                  triage_verdicts: dict[str, dict],
                  frequency: DocumentFrequency) -> tuple[list[Group], dict]:
    """Group the surviving products and elect a winner in each.

    Cheapest offer wins, and on an exact price tie the better commission. An exact tie on
    both falls back to ``item_key``, so the order is total: two runs on one corpus elect
    the same winner, which is what makes a merge replayable rather than merely
    reproducible.
    """
    views_by_key = {view.item_key: view for view in views}
    edges, edges_info = _merge_edges(views_by_key, decisions, triage_verdicts, frequency)

    union = _Union(tuple(sorted(views_by_key)))
    for left, right, _ in edges:
        union.union(left, right)

    rule_of_pair = {pair_id(left, right): rule for left, right, rule in edges}

    groups: list[Group] = []
    seen: set[str] = set()
    for key in sorted(views_by_key):
        root = union.find(key)
        if root in seen:
            continue
        seen.add(root)
        members = union.members(root)
        winner = sorted(members, key=lambda member: winner_key(views_by_key[member]))[0]
        if len(members) == 1:
            groups.append(Group(f"g-{winner}", members, winner, "NO_CANDIDATE"))
            continue
        inner = sorted(
            (identifier, rule) for identifier, rule in rule_of_pair.items()
            if identifier.split("__")[0] in members and identifier.split("__")[1] in members
        )
        groups.append(Group(
            f"g-{winner}", members, winner,
            inner[0][1] if inner else RULE_CONSOLIDATE,
            tuple(f"{rule} via {identifier}" for identifier, rule in inner),
        ))
    groups.sort(key=lambda group: group.group_id)
    return groups, edges_info


# ───────────────────────── consolidation (R-027) ─────────────────────────


def payment_methods(text: str) -> frozenset[str]:
    """Which payment methods a line names, individually.

    Per method and not per line, which is the whole difference. A loser writing
    "USD, CUP" and a winner writing "USD" share one of two methods, and R-027's exception
    is per method: the winner never declared CUP, so CUP must not arrive. Asking the
    question per line let the shared USD wave CUP through.
    """
    folded = fold_text(text)
    return frozenset(match.group(1).lower() for match in PAYMENT_TOKEN.finditer(folded))


def _declares_payment(winner: ProductView, raw: str) -> bool:
    """Did the winner already declare *every* payment method in this line?

    R-027's one exception: a payment method may transfer only if the receiving vendor
    already had it declared. Checking rather than assuming is the point -- and it is worth
    noticing what the exception can do. A method the winner lacks stays withheld, so the
    exception can never introduce a method the winner did not have. It lets a merge
    *keep* a method and never lets it *gain* one.
    """
    wanted = payment_methods(raw)
    if not wanted:
        return False
    declared = frozenset()
    for axis in winner.axes:
        declared |= payment_methods(axis.raw)
    return bool(declared) and wanted <= declared


def _consolidate_winner(views_by_key: dict[str, ProductView], group: Group) -> dict:
    """R-027 applied to one winner: its own terms, plus generic detail it did not have.

    The rule is negative and easy to get backwards. Provider-scoped values -- payment
    methods, delivery, messaging, warranty, paperwork, add-ons -- belong to the vendor
    that announced them and are **not** copied onto another vendor's product, whatever the
    price was: "no queremos malinformar al cliente". Generic technical detail may cross,
    and when it does it keeps the ``source_provider`` of whoever wrote it, so the owner's
    card can show that a line came from a vendor other than the one being sold. That is
    also what ``agent_catalog.assert_provenance`` permits: a ``provider_scoped`` node
    carrying a foreign key fails the run, while a ``generic`` node may carry any provider
    the run declared.
    """
    winner = views_by_key[group.winner]
    losers = [views_by_key[member] for member in group.members if member != group.winner]

    details: list[dict] = []
    conditions: list[dict] = []
    transferred: list[dict] = []
    withheld: list[dict] = []
    winner_keys = {axis.key for axis in winner.comparable_axes if axis.key}

    for axis in winner.axes:
        node = {"title": axis.label or axis.value[:60],
                "details": axis.value if axis.label else None,
                "raw": axis.raw, "scope": axis.scope,
                "source_provider": winner.provider_key}
        (conditions if axis.scope == "provider_scoped" else details).append(node)

    for loser in losers:
        for axis in loser.axes:
            if axis.scope == "provider_scoped":
                if axis.role == "separates":
                    note = "an add-on never transfers (R-027)"
                elif is_payment(axis.raw) and not _declares_payment(winner, axis.raw):
                    note = "payment method the winner never declared; not invented"
                else:
                    note = "provider-scoped term; the winner keeps its own"
                withheld.append({"item_key": loser.item_key, "axis": axis.kind, "note": note})
                continue
            if axis.key and axis.key in winner_keys:
                continue
            details.append({"title": axis.label or axis.value[:60],
                            "details": axis.value if axis.label else None,
                            "raw": axis.raw, "scope": "generic",
                            "source_provider": loser.provider_key})
            transferred.append({"item_key": loser.item_key, "axis": axis.key or axis.value[:40],
                                "from": loser.provider_key})

    return {
        "item_key": winner.item_key,
        "provider_key": winner.provider_key,
        "name": winner.name,
        "price": winner.price,
        "commission": winner.commission,
        "absorbed": sorted(loser.item_key for loser in losers),
        "details": details,
        "conditions": conditions,
        "transferred": transferred,
        "withheld": withheld,
    }


# ───────────────────────── the run ─────────────────────────


@dataclass
class Comparison:
    """The whole of step 2's output: decisions, groups, dispositions, arithmetic."""

    views: tuple[ProductView, ...]
    decisions: tuple[PairDecision, ...]
    groups: tuple[Group, ...]
    dispositions: dict[str, dict]
    merged: dict[str, dict]
    funnel: dict
    triage_queue: tuple[dict, ...]
    refused_edges: dict

    def as_dict(self) -> dict:
        return {
            "engine_version": ENGINE_VERSION,
            "constants": {
                "price_tiebreak_rel": PRICE_TIEBREAK_REL,
                "merge_floor": MERGE_FLOOR,
                "candidate_floor": CANDIDATE_FLOOR,
            },
            "funnel": self.funnel,
            "triage_queue": list(self.triage_queue),
            "decisions": [decision.as_dict() for decision in self.decisions],
            "groups": [
                {"group_id": group.group_id, "winner": group.winner,
                 "members": list(group.members), "basis": group.basis,
                 "notes": list(group.notes)}
                for group in self.groups
            ],
            "dispositions": [self.dispositions[key] for key in sorted(self.dispositions)],
            "merged": [self.merged[key] for key in sorted(self.merged)],
        }


def _dedupe_within_source(views: list[ProductView]) -> int:
    """Mark literal repeats inside one vendor's own chat.

    The legacy pipeline dropped 66 of 215 products in exactly this shape -- same name,
    same price, same action, same target -- with no row, no warning and no
    ``warnings_log``; the report read "149 decisiones (de 215 productos parseados)" and
    "Avisos: (ninguno)". Here the collapse is explicit, the survivor is the earliest
    ordinal so the choice replays, and every collapsed product leaves with the reason the
    schema already has a value for.
    """
    first_seen: dict[tuple, str] = {}
    duplicates = 0
    for view in sorted(views, key=lambda item: (item.provider_key, item.ordinal)):
        key = view.fingerprint
        survivor = first_seen.setdefault(key, view.item_key)
        if survivor != view.item_key:
            view.duplicate_of = survivor
            duplicates += 1
    return duplicates


def _candidate_pairs(views: list[ProductView], floor: float,
                     frequency: DocumentFrequency) -> tuple[list[tuple], dict]:
    """Every cross-vendor pair at or above the floor, plus what fell below it.

    Quadratic on purpose. The run's product count is bounded by what the owner uploaded,
    and an index would buy speed at the price of pairs nobody ever looks at -- the exact
    failure this stage exists to stop. A pair under the floor is reported with the best
    score it reached, so "why are these two not merged" is answerable.
    """
    pairs: list[tuple] = []
    best_seen: dict[str, float] = {}
    scored = 0
    for index, left in enumerate(views):
        best = 0.0
        for right in views[index + 1:]:
            if left.provider_key == right.provider_key:
                continue
            scored += 1
            score = name_score(left, right, frequency)
            if score > best:
                best = score
            if score >= floor:
                pairs.append((left, right, score))
        best_seen[left.item_key] = round(best, 6)
    pairs.sort(key=lambda entry: (-entry[2], entry[0].item_key, entry[1].item_key))
    return pairs, best_seen, scored


def _tally_triage(decisions: tuple[PairDecision, ...],
                  triage_verdicts: dict[str, dict]) -> dict:
    """Count what the model did, and what it did not.

    An absent pair, a duplicated pair, or an answer outside the closed vocabulary is
    ``unresolved``, and unresolved means separated. That is the conservative reading
    applied to silence: no answer is not permission to merge.
    """
    tally = {"same": 0, "different": 0, "unresolved": 0, "overruled": 0}
    overruled: list[dict] = []
    for decision in decisions:
        answer = triage_verdicts.get(decision.pair_id)
        if decision.verdict != TRIAGE:
            if answer is not None and answer.get("verdict") == TRIAGE_SAME:
                # The gate is one-directional and it is enforced here as well as in
                # triage.py, because the artifact is what the next phase reads.
                tally["overruled"] += 1
                overruled.append({"pair_id": decision.pair_id, "code_rule": decision.rule})
            continue
        verdict = (answer or {}).get("verdict")
        if verdict == TRIAGE_SAME:
            tally["same"] += 1
        elif verdict == TRIAGE_DIFFERENT:
            tally["different"] += 1
        else:
            tally["unresolved"] += 1
    tally["_overruled"] = overruled
    return tally


def reconcile(funnel: dict, dispositions: dict[str, dict],
              input_keys: tuple[str, ...]) -> None:
    """The assertion this file is arranged around.

    Every input product leaves with exactly one disposition and the counts close in three
    independent directions: by product, by pair, and by triage outcome. The legacy run
    reported 149 decisions from 215 parsed products with nothing recorded for the other
    66. That was not a warning sign, it was the only number there was. Here, if a product
    has no disposition, or the arithmetic does not close, this raises and the engine
    writes no artifact at all.
    """
    missing = sorted(set(input_keys) - set(dispositions))
    extra = sorted(set(dispositions) - set(input_keys))
    if missing or extra:
        raise FunnelUnreconciled(
            f"{len(missing)} ingested product(s) have no disposition and {len(extra)} "
            f"disposition(s) name a product that was never ingested; "
            f"missing={missing[:10]} extra={extra[:10]}"
        )

    tallies: dict[str, int] = {}
    for key in sorted(dispositions):
        disposition = dispositions[key]["disposition"]
        tallies[disposition] = tallies.get(disposition, 0) + 1

    accounted = (tallies.get(WINNER, 0) + tallies.get(STANDALONE, 0)
                 + tallies.get(CONSOLIDATED, 0) + tallies.get(DUPLICATE, 0))
    if accounted != len(input_keys):
        raise FunnelUnreconciled(
            f"dispositions sum to {accounted} over {len(input_keys)} ingested products "
            f"({tallies}); a product left the run with no recorded reason"
        )
    produced = tallies.get(WINNER, 0) + tallies.get(STANDALONE, 0)
    absorbed = tallies.get(CONSOLIDATED, 0)
    if funnel["products_out"] != produced:
        raise FunnelUnreconciled(
            f"{funnel['products_out']} groups for {produced} products that continue; "
            "a group does not correspond to one product"
        )
    if produced + absorbed != len(input_keys) - tallies.get(DUPLICATE, 0):
        raise FunnelUnreconciled(
            f"{produced} products out + {absorbed} absorbed is not "
            f"{len(input_keys) - tallies.get(DUPLICATE, 0)} products that were not "
            "duplicates; a product was neither kept nor consolidated"
        )

    pairs = funnel["pairs"]
    partition = (funnel["pairs_different_addon"] + funnel["pairs_different_categorical"]
                 + funnel["pairs_different_no_price"] + funnel["pairs_triage"]
                 + funnel["pairs_same_deterministic"])
    if pairs != partition:
        raise FunnelUnreconciled(
            f"{pairs} candidate pairs partition into {partition}; a pair has no verdict"
        )

    triage = funnel["pairs_triage"]
    resolved = funnel["triage_same"] + funnel["triage_different"] + funnel["triage_unresolved"]
    if triage != resolved:
        raise FunnelUnreconciled(
            f"{triage} triaged pairs partition into {resolved}; a triaged pair has no outcome"
        )


def run(items: list[dict], triage_verdicts: dict[str, dict] | None = None,
        floor: float = CANDIDATE_FLOOR) -> Comparison:
    """Step 2 end to end over an already-ingested list.

    ``triage_verdicts`` is what ``triage.py`` produced; passing it is how a pair the
    engine could not decide becomes a decision. Without it every triaged pair is
    unresolved, which the gate reads the conservative way -- separated, and counted.
    """
    triage_verdicts = triage_verdicts or {}
    every_view = [build_view(record) for record in items]
    duplicates = _dedupe_within_source(every_view)
    views = [view for view in every_view if view.duplicate_of is None]
    views.sort(key=lambda view: (view.provider_key, view.ordinal))
    views_by_key = {view.item_key: view for view in views}
    input_keys = tuple(sorted(record["item_key"] for record in items))

    # One document-frequency table for the whole run. It is a function of the input set
    # and of nothing else, so two runs over the same corpus weigh every token identically
    # and produce the same candidate list in the same order.
    frequency = DocumentFrequency(views)
    pairs, best_seen, scored = _candidate_pairs(views, floor, frequency)
    decisions = tuple(decide_pair(left, right, score) for left, right, score in pairs)
    groups, edges_info = _build_groups(views, decisions, triage_verdicts, frequency)
    tally = _tally_triage(decisions, triage_verdicts)

    winner_of: dict[str, str] = {}
    for group in groups:
        for member in group.members:
            if member != group.winner:
                winner_of[member] = group.winner
    group_of = {group.winner: group for group in groups}

    merged = {group.winner: _consolidate_winner(views_by_key, group) for group in groups}

    dispositions: dict[str, dict] = {}
    # Every ingested product, including the ones collapsed inside one vendor's chat. The
    # first draft walked only the surviving views and handed the duplicates no
    # disposition at all -- which is precisely the failure this file exists to prevent,
    # reproduced inside the file that was written to prevent it. ``reconcile`` caught it
    # on the first run over three records; that is what the assertion is for.
    for view in every_view:
        key = view.item_key
        if view.duplicate_of is not None:
            dispositions[key] = {
                "item_key": key, "provider_key": view.provider_key,
                "disposition": DUPLICATE, "reason": REASON_DUPLICATE,
                "detail": f"identical offer to {view.duplicate_of} in the same chat; "
                          "the earliest ordinal survives",
                "group_id": None, "winner_item_key": view.duplicate_of,
            }
            continue
        group = next(item for item in groups if key in item.members)
        if key in winner_of:
            dispositions[key] = {
                "item_key": key, "provider_key": view.provider_key,
                "disposition": CONSOLIDATED, "reason": REASON_CONSOLIDATED,
                "detail": f"same product as {winner_of[key]}, which wins on price and "
                          "only on commission when the prices are equal",
                "group_id": group.group_id, "winner_item_key": winner_of[key],
            }
        elif len(group.members) > 1:
            dispositions[key] = {
                "item_key": key, "provider_key": view.provider_key,
                "disposition": WINNER, "reason": REASON_NONE,
                "detail": f"wins a group of {len(group.members)}",
                "group_id": group.group_id, "winner_item_key": key,
            }
        else:
            best = best_seen.get(key, 0.0)
            below = best < floor
            dispositions[key] = {
                "item_key": key, "provider_key": view.provider_key,
                "disposition": STANDALONE,
                "reason": REASON_NO_CANDIDATE if below else REASON_NONE,
                "detail": (f"best cross-provider name similarity {best:.2f} is under the "
                           f"candidate floor {floor:.2f}" if below else
                           "every candidate pair against it was decided different"),
                "group_id": group.group_id, "winner_item_key": key,
            }

    queue = [
        triage_evidence(views_by_key[decision.left], views_by_key[decision.right], decision)
        for decision in decisions if decision.verdict == TRIAGE
    ]

    by_rule: dict[str, int] = {}
    for decision in decisions:
        by_rule[decision.rule] = by_rule.get(decision.rule, 0) + 1

    continuing = sum(1 for d in dispositions.values() if d["disposition"] == STANDALONE)
    funnel = {
        "parsed": len(input_keys),
        "duplicates_in_source": duplicates,
        "after_duplicate_drop": len(input_keys) - duplicates,
        "cross_provider_pairs_scored": scored,
        "pairs": len(pairs),
        "pairs_different_addon": by_rule.get(RULE_ADDON, 0),
        "pairs_different_categorical": by_rule.get(RULE_CATEGORICAL, 0),
        "pairs_different_no_price": by_rule.get(RULE_NO_PRICE, 0),
        "pairs_triage": sum(1 for decision in decisions if decision.verdict == TRIAGE),
        "pairs_same_deterministic": sum(1 for decision in decisions if decision.verdict == SAME),
        "triage_same": tally["same"],
        "triage_different": tally["different"],
        "triage_unresolved": tally["unresolved"],
        "triage_overruled": tally["overruled"],
        "triage_rules": {rule: by_rule[rule] for rule in sorted(by_rule)
                         if rule.startswith(("NUMERIC", "UNIT", "MIXED", "PRICE", "THIN"))},
        "merge_edges_applied": edges_info["applied"],
        "merge_edges_refused": len(edges_info["refused"]),
        "refused_edges": edges_info["refused"],
        "groups": len(groups),
        "groups_multi": sum(1 for group in groups if len(group.members) > 1),
        "winners": sum(1 for d in dispositions.values() if d["disposition"] == WINNER),
        "consolidated": sum(1 for d in dispositions.values() if d["disposition"] == CONSOLIDATED),
        "standalone": continuing,
        "products_out": len(groups),
    }
    reconcile(funnel, dispositions, input_keys)

    return Comparison(
        views=tuple(views), decisions=decisions, groups=tuple(groups),
        dispositions=dispositions, merged=merged, funnel=funnel,
        triage_queue=tuple(queue), refused_edges=edges_info["refused"],
    )


def digest(payload: dict) -> str:
    """A sha256 over the artifact, so a rerun is one diff instead of a comparison."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _describe(funnel: dict) -> None:
    """The funnel as arithmetic, every decrement naming itself."""
    print(f"parsed                        {funnel['parsed']:>5d}")
    print(f"  − duplicate in own chat     {funnel['duplicates_in_source']:>5d}"
          f"   ({REASON_DUPLICATE})")
    print(f"  = examined for pairs        {funnel['after_duplicate_drop']:>5d}")
    print(f"\ncross-provider pairs ≥ floor  {funnel['pairs']:>5d}")
    print(f"  → different · add-on        {funnel['pairs_different_addon']:>5d}"
          f"   ({RULE_ADDON})")
    print(f"  → different · categorical   {funnel['pairs_different_categorical']:>5d}"
          f"   ({RULE_CATEGORICAL})")
    print(f"  → different · no price      {funnel['pairs_different_no_price']:>5d}"
          f"   ({RULE_NO_PRICE})")
    print(f"  → triage                    {funnel['pairs_triage']:>5d}")
    for rule in sorted(funnel["triage_rules"]):
        print(f"       because {rule:<28} {funnel['triage_rules'][rule]:>5d}")
    print(f"       of which same={funnel['triage_same']} "
          f"different={funnel['triage_different']} "
          f"unresolved={funnel['triage_unresolved']} "
          f"overruled={funnel['triage_overruled']}")
    print(f"  → same, no model            {funnel['pairs_same_deterministic']:>5d}"
          f"   ({RULE_CONSOLIDATE})")
    print(f"\nmerge edges applied           {funnel['merge_edges_applied']:>5d}")
    print(f"merge edges refused           {funnel['merge_edges_refused']:>5d}"
          f"   (a member pair contradicted)")
    print(f"\ngroups                        {funnel['groups']:>5d}"
          f"   ({funnel['groups_multi']} with more than one member)")
    print(f"winners                       {funnel['winners']:>5d}")
    print(f"consolidated                  {funnel['consolidated']:>5d}"
          f"   ({REASON_CONSOLIDATED})")
    print(f"standalone                    {funnel['standalone']:>5d}")
    print(f"\n{funnel['winners']} winners + {funnel['consolidated']} consolidated + "
          f"{funnel['standalone']} standalone = "
          f"{funnel['winners'] + funnel['consolidated'] + funnel['standalone']} "
          f"against {funnel['parsed']} parsed "
          f"(−{funnel['duplicates_in_source']} duplicates)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Consolidate products between providers without touching the database."
    )
    parser.add_argument("--items", required=True, help="items.json written by ingest.py")
    parser.add_argument("--out", required=True, help="where to write consolidation.json")
    parser.add_argument(
        "--triage", default=None,
        help="triage-verdicts.json from triage.py; without it every triaged pair stays "
             "undecided, which is counted as unresolved and separated",
    )
    parser.add_argument("--floor", type=float, default=CANDIDATE_FLOOR,
                        help=f"name-similarity floor for becoming a candidate "
                             f"(default {CANDIDATE_FLOOR})")
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.items).read_text(encoding="utf-8"))
    triage_verdicts: dict[str, dict] = {}
    if args.triage:
        triage_payload = json.loads(Path(args.triage).read_text(encoding="utf-8"))
        triage_verdicts = {entry["pair_id"]: entry
                           for entry in triage_payload.get("verdicts", [])}

    try:
        comparison = run(payload["items"], triage_verdicts, floor=args.floor)
    except CompareRejected as exc:
        print(f"comparison refused to emit a list: {exc}", file=sys.stderr)
        return 1

    document = comparison.as_dict()
    document["source_shape_signature"] = payload.get("shape_signature", "")
    document["digest"] = digest(document)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )

    _describe(comparison.funnel)
    print(f"\ndigest {document['digest'][:16]} → {out_path}")
    print(f"{len(comparison.triage_queue)} pairs awaiting the model")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())