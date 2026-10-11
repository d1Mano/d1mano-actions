"""Turn a verdict into the exact write it implies (R-026, R-027, R-028, R-033).

``dbface`` decides *whether* a published product changes. This decides *what* changes,
and it is deliberately narrow about it:

* **Price, supplier and commission** are the offer. They are what an update is.
* **Commercial terms** -- transport, warranty, paperwork, payment methods -- belong to
  the supplier who stated them (R-027). When the supplier of a product changes, the
  previous supplier's terms are no longer true and staying silent about that is the
  one thing R-028 names as unacceptable: *no queremos malinformar al cliente*. So the
  old terms leave with the old supplier, and the new supplier's own stated terms take
  their place. Nothing is carried across and nothing is invented: a term the new
  supplier did not state is simply absent.
* **Everything else is left alone.** Name, description, images and the technical sheet
  of a published product were curated by a person; an existing product "brings its
  own" (R-010). This module has no code path that writes them.

A touch (R-033) is the smallest write there is: the product is confirmed still on
offer, and nothing about it changes.

No icon, title catalogue or unit list of the app is embedded here. The four term
titles are plain trade Spanish; the icon for each kind is learned at run time from
what the catalog itself already uses for that kind of term.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections import Counter
from typing import Any, Mapping, Sequence

from . import compare as _compare
from .dbface import ACTION_TOUCH, ACTION_UPDATE, CatalogEntry, Verdict

# ---------------------------------------------------------------------------
# Commercial terms
# ---------------------------------------------------------------------------

TRANSPORT = "transport"
WARRANTY = "warranty"
DOCUMENTS = "documents"
PAYMENT = "payment"

#: Order is display order, and the order ties are broken in.
KINDS = (TRANSPORT, WARRANTY, DOCUMENTS, PAYMENT)

TITLES = {
    TRANSPORT: "Transporte",
    WARRANTY: "Garantía",
    DOCUMENTS: "Documentación",
    PAYMENT: "Formas de pago",
}

# Matched against accent-folded, lower-cased text. Narrower than
# ``compare.COMMERCIAL_PHRASES`` on purpose: that list decides what must NOT count as a
# technical difference, where a false positive costs nothing. This one decides what gets
# written into a published product, where a false positive ("encendido por tarjeta",
# "transfer automático") would publish a specification as a payment method.
_KIND_PATTERNS = {
    TRANSPORT: re.compile(
        r"\btranspor?t[ea]\b|\bmensajeri[ao]\b|\bdomicilio\b|\bentregas?\b|\benvios?\b"
        r"|\bdelivery\b|\brecogid[ao]\b|\breparto\b"),
    WARRANTY: re.compile(r"\bgaranti[ao]s?\b"),
    DOCUMENTS: re.compile(
        r"\bpapel(?:es)?\b|\bfacturas?\b|\bfacturacion\b|\bdocumentacion\b|\bdocumentos\b"
        r"|\btraspaso\b|\binscripcion\b|\baduana\b"),
    PAYMENT: re.compile(
        r"\bformas? de pago\b|\bpagos?\b|\bmonedas? aceptadas?\b|\bzelle\b|\busd\b|\beur\b"
        r"|\bmn\b|\bmlc\b|\bcup\b|\befectivo\b"),
}

# The words that only announce the kind and carry no content of their own. Stripped
# from the front of a line so "GARANTÍA: 15 DÍAS" publishes as "15 días".
_KIND_LABELS = {
    TRANSPORT: re.compile(r"^(?:con\s+)?(?:transpor?t[ea]|mensajeri[ao])\b[\s:.-]*", re.I),
    WARRANTY: re.compile(r"^garanti[ao]s?\b(?:\s+de\b)?[\s:.-]*", re.I),
    DOCUMENTS: re.compile(r"^(?:documentacion|documentos)\b[\s:.-]*", re.I),
    PAYMENT: re.compile(r"^(?:formas? de pago|monedas? aceptadas?|pagos?)\b[\s:.-]*", re.I),
}

# Tokens whose capitalisation carries meaning and must survive sentence-casing.
_KEEP_UPPER = {"usd", "eur", "mn", "mlc", "cup", "mipyme", "mypime", "mipime"}
_KEEP_TITLE = {"zelle": "Zelle", "habana": "Habana", "artemisa": "Artemisa"}

TITLE_MAX = 100
DETAILS_MAX = 500

_NAMESPACE = uuid.UUID("6f0a1c1e-7f0b-4c38-9b7e-0c8d4a2d51aa")


def classify(text: str) -> str | None:
    """Which kind of commercial term a line states, or ``None`` when it states none."""
    folded = _compare.fold_text(text)
    for kind in KINDS:
        if _KIND_PATTERNS[kind].search(folded):
            return kind
    return None


def _strip_symbols(text: str) -> str:
    """Drop emoji, bullets and decoration; keep words, digits and plain punctuation."""
    kept: list[str] = []
    for char in unicodedata.normalize("NFKC", text):
        category = unicodedata.category(char)
        if category[0] in ("L", "N") or char in " ,.;:/()%-+":
            kept.append(char)
        else:
            kept.append(" ")
    return re.sub(r"\s+", " ", "".join(kept)).strip(" ,.;:-")


def _sentence_case(text: str) -> str:
    """Lower-case a shouted line without flattening the tokens that are acronyms."""
    if not text or not any(char.isupper() for char in text):
        return text
    if any(char.islower() for char in text):
        return text[0].upper() + text[1:]
    words = []
    for word in text.lower().split(" "):
        bare = word.strip(",.;:()")
        folded = _compare.fold_text(bare)
        if folded in _KEEP_UPPER:
            word = word.replace(bare, bare.upper())
        elif folded in _KEEP_TITLE:
            word = word.replace(bare, _KEEP_TITLE[folded])
        words.append(word)
    out = " ".join(words)
    return out[0].upper() + out[1:] if out else out


def _fold_plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn")


def term_text(kind: str, raw: str) -> str:
    """The publishable text of one supplier line, with the announcing label removed."""
    cleaned = _strip_symbols(raw)
    # The label regexes are written without accents; match on a folded copy and cut the
    # same number of characters from the original, which NFD-folding preserves.
    match = _KIND_LABELS[kind].match(_fold_plain(cleaned))
    if match:
        cleaned = cleaned[match.end():].strip(" ,.;:-")
    return _sentence_case(cleaned)[:DETAILS_MAX]


def stated_terms(conditions: Sequence[Mapping[str, Any]], provider_key: str) -> dict[str, str]:
    """What this supplier itself stated, per kind, in the order it stated it.

    Only lines tagged with the product's own supplier are read. The consolidation
    already withholds other suppliers' terms; checking the tag again here means a
    mistake upstream cannot become a published warranty nobody offered (R-027).
    """
    parts: dict[str, list[str]] = {}
    for node in conditions:
        if node.get("source_provider") != provider_key:
            continue
        raw = str(node.get("raw") or "")
        kind = classify(raw)
        if kind is None:
            continue
        text = term_text(kind, raw)
        if not text:
            continue  # a bare header such as "FORMAS DE PAGO": announces, says nothing
        bucket = parts.setdefault(kind, [])
        if _compare.fold_text(text) not in {_compare.fold_text(p) for p in bucket}:
            bucket.append(text)
    return {kind: "; ".join(texts)[:DETAILS_MAX] for kind, texts in parts.items()}


def learn_icons(catalog: Sequence[CatalogEntry]) -> dict[str, str | None]:
    """The icon this catalog already uses for each kind of term.

    Learned from the published rows rather than declared here, so the app's icon set is
    never copied into this repository and a business that uses different icons gets its
    own. A kind the catalog has never used gets no icon, which the app renders fine.
    """
    counts: dict[str, Counter] = {kind: Counter() for kind in KINDS}
    for entry in catalog:
        for row in entry.conditions:
            kind = classify(row.get("title") or "")
            icon = row.get("icon")
            if kind is not None and icon:
                counts[kind][str(icon)] += 1
    learned: dict[str, str | None] = {}
    for kind in KINDS:
        ranked = sorted(counts[kind].items(), key=lambda pair: (-pair[1], pair[0]))
        learned[kind] = ranked[0][0] if ranked else None
    return learned


def _condition_id(run_id: str, item_key: str, kind: str) -> str:
    # Deterministic so a replayed write lands on the same row instead of adding one.
    return str(uuid.uuid5(_NAMESPACE, f"{run_id}:{item_key}:{kind}"))


def plan_conditions(
    run_id: str,
    item_key: str,
    entry: CatalogEntry,
    stated: Mapping[str, str],
    provider_changes: bool,
    icons: Mapping[str, str | None],
) -> tuple[list[str], list[dict], list[dict]]:
    """Which term rows leave, which arrive, and a readable list of the difference."""
    existing: dict[str, list[dict]] = {}
    for row in entry.conditions:
        kind = classify(row.get("title") or "")
        if kind is not None:
            existing.setdefault(kind, []).append(row)

    delete: list[str] = []
    insert: list[dict] = []
    changes: list[dict] = []
    next_order = max((row["sort_order"] for row in entry.conditions), default=-1) + 1

    for kind in KINDS:
        rows = existing.get(kind, [])
        text = stated.get(kind)
        before = "; ".join(str(row.get("details") or row.get("title") or "") for row in rows) or None

        if text is None:
            # The new supplier said nothing about this. Under the same supplier the
            # published term stands; under a new one it is somebody else's promise.
            if provider_changes and rows:
                delete.extend(row["id"] for row in rows)
                changes.append({"field": f"condition:{kind}", "label": TITLES[kind],
                                "from": before, "to": None})
            continue

        if len(rows) == 1 and (
            _compare.fold_text(str(rows[0].get("details") or "")) == _compare.fold_text(text)
        ):
            # The published term already says this. Under the same supplier nothing
            # moved; under a new one, the new supplier stated the very same thing, so
            # the row is true of them too and there is nothing to rewrite.
            continue

        delete.extend(row["id"] for row in rows)
        if rows:
            order = min(row["sort_order"] for row in rows)
            icon = next((row.get("icon") for row in rows if row.get("icon")), None) or icons.get(kind)
        else:
            order, next_order = next_order, next_order + 1
            icon = icons.get(kind)
        insert.append({
            "id": _condition_id(run_id, item_key, kind),
            "icon": icon,
            "title": TITLES[kind][:TITLE_MAX],
            "details": text,
            "sort_order": order,
        })
        changes.append({"field": f"condition:{kind}", "label": TITLES[kind],
                        "from": before, "to": text})

    return delete, insert, changes


# ---------------------------------------------------------------------------
# Commission
# ---------------------------------------------------------------------------

FREE_COMMISSION_NOTE = "Comisión libre"


def commission_text(commission: Mapping[str, Any] | None) -> str | None:
    if not commission:
        return None
    if commission.get("free"):
        return "libre"
    value = commission.get("value")
    return None if value is None else f"{float(value):g}"


def plan_commission(incoming: Mapping[str, Any] | None, published: Mapping[str, Any] | None,
                    provider_changes: bool) -> tuple[dict | None, bool, dict | None]:
    """``(row to upsert, clear?, change)`` for the commission of one product."""
    if _compare.commission_score(dict(incoming) if incoming else None) == \
            _compare.commission_score(dict(published) if published else None):
        return None, False, None

    change = {"field": "commission", "label": "Comisión",
              "from": commission_text(published), "to": commission_text(incoming)}

    if not incoming or (not incoming.get("free") and incoming.get("value") is None):
        # Nothing stated. A commission is an agreement with one supplier; it does not
        # survive a change of supplier, and it is not touched under the same one.
        if provider_changes and published:
            return None, True, change
        return None, False, None

    if incoming.get("free"):
        return {"commission_type": "fixed", "value": 0, "notes": FREE_COMMISSION_NOTE}, False, change
    return {"commission_type": "fixed", "value": float(incoming["value"]), "notes": None}, False, change


# ---------------------------------------------------------------------------
# The proposal
# ---------------------------------------------------------------------------


def build(
    run_id: str,
    verdict: Verdict,
    offer: Mapping[str, Any],
    conditions: Sequence[Mapping[str, Any]],
    entry: CatalogEntry | None,
    icons: Mapping[str, str | None],
) -> dict | None:
    """The write one verdict implies, or ``None`` when it implies none.

    The result is stored with the item and is what the owner approves: ``changes`` is
    the human-readable difference, and the remaining keys are exactly what the apply
    step sends. What is approved and what is applied are the same object.
    """
    if verdict.action == ACTION_TOUCH:
        return {"item_key": verdict.item_key, "product_id": verdict.product_id,
                "action": ACTION_TOUCH, "changes": []}
    if verdict.action != ACTION_UPDATE or entry is None:
        return None

    changes: list[dict] = []
    product: dict[str, Any] = {}

    price = offer.get("price")
    if price is not None and price != entry.price:
        product["base_price"] = float(price)
        changes.append({"field": "base_price", "label": "Precio",
                        "from": entry.price, "to": float(price)})
    if verdict.provider_changes:
        product["provider_public_key"] = verdict.provider_key
        changes.append({"field": "provider_public_key", "label": "Proveedor",
                        "from": entry.provider_key, "to": verdict.provider_key})

    commission, clear, commission_change = plan_commission(
        offer.get("commission"), entry.commission, verdict.provider_changes)
    if commission_change is not None:
        changes.append(commission_change)

    stated = stated_terms(conditions, verdict.provider_key)
    delete, insert, term_changes = plan_conditions(
        run_id, verdict.item_key, entry, stated, verdict.provider_changes, icons)
    changes.extend(term_changes)

    row: dict[str, Any] = {
        "item_key": verdict.item_key,
        "product_id": verdict.product_id,
        "action": ACTION_UPDATE,
        # Always an object, even when only terms or the commission move: the write
        # refreshes the product either way, because the offer was confirmed today.
        "product": product,
        "changes": changes,
    }
    if commission is not None:
        row["commission"] = commission
    if clear:
        row["commission_clear"] = True
    if delete:
        row["conditions_delete"] = sorted(delete)
    if insert:
        row["conditions_insert"] = insert
    return row


#: The keys of a proposal that travel to the database. ``changes`` is for people.
WRITE_KEYS = ("item_key", "product_id", "action", "product", "commission",
              "commission_clear", "conditions_delete", "conditions_insert")


def write_row(proposal: Mapping[str, Any]) -> dict:
    """Strip a stored proposal down to what ``apply_products`` accepts."""
    return {key: proposal[key] for key in WRITE_KEYS if key in proposal}


__all__ = [
    "KINDS",
    "TITLES",
    "TRANSPORT",
    "WARRANTY",
    "DOCUMENTS",
    "PAYMENT",
    "FREE_COMMISSION_NOTE",
    "WRITE_KEYS",
    "classify",
    "term_text",
    "stated_terms",
    "learn_icons",
    "plan_conditions",
    "plan_commission",
    "build",
    "write_row",
]
