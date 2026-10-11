"""Step 3: confront the consolidated list against the published catalog.

``compare.py`` answered "are these two supplier offers the same product" among the
suppliers. This answers the next question, against what is already published: does
this product exist, and is what arrived worth changing it for.

It produces **decisions, not writes**. Nothing here opens a socket or mutates anything;
the catalog arrives as rows somebody else read, and what leaves is one verdict per
incoming product. A product with no counterpart is returned to the client and never
inserted (R-011).

Three questions, in this order, and the order is the safety property
--------------------------------------------------------------------
1. **Is it the same product?** Decided on the words of the two names, not on how
   alike they look: an omission is not a difference, a contradiction is (see "Name
   identity" below). Then a measurement that differs, a categorical property that
   contradicts, a declared quantity that does not agree, an add-on on one side only --
   any of these also means *not the same product*, and the candidate is dropped. The default is separation (R-024): a
   wrong merge overwrites a published product with another one's price, a wrong split
   only sends a product back to the owner as new.
2. **Whose offer is it?** The same supplier re-announcing its own product is not a
   competition, it is news. Whatever that supplier says now is the truth about its
   offer, up or down. A different supplier is a competing offer and has to win.
3. **Who wins on money?** Price first, without exception. Commission only on an exact
   price tie (R-026). Staleness overrides *price*, never *identity* (R-028): a row
   nobody confirmed in 48 hours yields to a fresh offer of the same thing.

One catalog product, one incoming product
-----------------------------------------
Two incoming products can both look like the same catalog row -- a base model and its
variant with an accessory are the usual case. Letting both write to it would publish
whichever ran last. So the pairing is an assignment: best score first, each catalog
row taken once, and the product that loses the row looks at its next candidate or
goes back to the client.

Why the staleness model is duplicated here
-------------------------------------------
The app owns this rule in TypeScript and the runner is Python in another repository,
so it is **ported**, not called, and the port is kept literal so the two cannot drift
in meaning. If the app's rule changes, this changes with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from . import compare as _compare
from .compare import ProductView, parse_quantity, quantities_agree

# ---------------------------------------------------------------------------
# R-028 · the staleness threshold
# ---------------------------------------------------------------------------

#: Hours of inactivity after which a catalog product yields to a fresh offer.
#:
#: Deliberately longer than the 36h at which the client already shows a risk badge.
#: The agent acts *after* the signal appeared, not before: the owner chose the margin.
STALE_HOURS = 48

#: Mirrors of the app's own thresholds, carried so the port is checkable against its
#: source at a glance, not because this module uses them.
RISK_HOURS_CLIENT = 36
RISK_HOURS_OWNER = 24

_HOURS_PER_MS = 3_600_000.0

#: A price that moved by more than this factor in either direction is far more likely
#: a parsing accident (a dropped digit, a price per unit read as a total) than a market
#: move. It is not applied by anybody's default; the owner sees it and decides.
IMPLAUSIBLE_PRICE_FACTOR = 5.0


class ConfrontationRejected(RuntimeError):
    """The confrontation cannot be trusted to have seen every input."""


class FunnelUnreconciled(ConfrontationRejected):
    """An input product left the output without a recorded disposition.

    The legacy matcher did exactly that once: a third of a supplier's list became no
    decision at all, with no warning anywhere. A shorter list that looks fine is the
    failure this refuses to reproduce.
    """


# ---------------------------------------------------------------------------
# Port of the app's staleness rule
# ---------------------------------------------------------------------------


def product_effective_date(created_at: str | None, updated_at: str | None) -> str | None:
    """When the product was last confirmed, as the app defines it.

    A product that was only ever *touched* has ``updated_at != created_at`` and reports
    the touch; a product nobody ever edited reports its creation. Either way a touch
    resets the clock, which is what makes R-033's refresh the mechanism that keeps a
    live product alive.
    """
    if updated_at and updated_at != created_at:
        return updated_at
    if created_at:
        return created_at
    return None


_FRACTION = re.compile(r"\.(\d+)")


def _parse_instant(value: str) -> float:
    """Read a Postgres timestamp.

    Postgres hands back variable-width fractional seconds -- five digits is a real case
    -- and ``fromisoformat`` wants exactly three or six. The fraction is normalised
    rather than rejected: a product that lost its clock over one digit would be
    *undated*, an undated product is never stale, and a product that is never stale
    never yields to a fresh offer.
    """
    text = value.strip().replace("Z", "+00:00")

    def _pad(match: re.Match[str]) -> str:
        return "." + (match.group(1) + "000000")[:6]

    text = _FRACTION.sub(_pad, text, count=1)
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _now_seconds() -> float:
    return datetime.now(timezone.utc).timestamp()


def product_stale_hours(
    created_at: str | None, updated_at: str | None, now: float | None = None
) -> int | None:
    """Hours of inactivity, or ``None`` when the row carries no timestamps.

    ``now`` is the instant the catalog snapshot was taken. Passing it makes a
    confrontation a function of the snapshot alone, so re-running it over the same
    snapshot an hour later reaches the same verdicts.
    """
    effective = product_effective_date(created_at, updated_at)
    if not effective:
        return None
    reference = _now_seconds() if now is None else now
    elapsed_ms = (reference - _parse_instant(effective)) * 1000.0
    if elapsed_ms <= 0:
        return 0
    return int(elapsed_ms // _HOURS_PER_MS)


def is_product_stale(
    created_at: str | None,
    updated_at: str | None,
    threshold_hours: int = STALE_HOURS,
    now: float | None = None,
) -> bool:
    """True when the product has sat unconfirmed past the threshold.

    A missing timestamp is ``False``, not an error. A product with no dates is not
    *stale*, it is *unknown*, and inventing a date for it would let an unknown row
    evict a live one.
    """
    hours = product_stale_hours(created_at, updated_at, now)
    return hours is not None and hours >= threshold_hours


# ---------------------------------------------------------------------------
# The catalog side, read into the language the comparison speaks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogEntry:
    """One product as the catalog already publishes it."""

    product_id: str
    provider_key: str | None
    name: str
    normalized_name: str
    tokens: tuple[str, ...]
    price: float | None
    commission: dict | None
    details: tuple[tuple[str, str], ...]
    conditions: tuple[dict, ...]
    created_at: str | None
    updated_at: str | None


def _price_of(row: Mapping[str, Any]) -> float | None:
    raw = row.get("base_price")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def commission_of_row(row: Mapping[str, Any] | None) -> dict | None:
    """A catalog commission row in the shape ``compare.commission_score`` reads.

    The catalog has no "free" column: a free commission is a row whose note says so.
    Read here, once, so the tie-break compares both sides in one vocabulary.
    """
    if not row:
        return None
    notes = str(row.get("notes") or "")
    folded = _compare.fold_text(notes)
    if "libre" in folded or "free" in folded:
        return {"free": True, "value": None, "notes": notes or None}
    raw = row.get("value")
    if raw is None:
        return None
    try:
        return {"free": False, "value": float(raw), "notes": notes or None}
    except (TypeError, ValueError):
        return None


def load_catalog(
    products: Sequence[Mapping[str, Any]],
    detail_rows: Sequence[Mapping[str, Any]] = (),
    commission_rows: Sequence[Mapping[str, Any]] = (),
    condition_rows: Sequence[Mapping[str, Any]] = (),
) -> tuple[CatalogEntry, ...]:
    """Read the catalog into entries this module can compare against.

    Ordered by ``id`` so the candidate walk is a function of the data alone. The caller
    is responsible for handing over one tenant's products and nothing else: this module
    cannot tell whose a row is, and a confrontation against another business's catalog
    produces a funnel that balances and is wrong.
    """
    details_by_product: dict[str, list[tuple[str, str]]] = {}
    for row in detail_rows:
        title = row.get("title")
        if not title:
            continue
        value = row.get("value")
        text = row.get("details") or ("" if value is None else str(value))
        details_by_product.setdefault(str(row.get("product_id")), []).append(
            (str(title), str(text).strip())
        )

    conditions_by_product: dict[str, list[dict]] = {}
    for row in condition_rows:
        conditions_by_product.setdefault(str(row.get("product_id")), []).append(
            {
                "id": str(row.get("id")),
                "icon": row.get("icon"),
                "title": str(row.get("title") or ""),
                "details": row.get("details"),
                "sort_order": int(row.get("sort_order") or 0),
            }
        )

    commission_by_product = {str(row.get("product_id")): row for row in commission_rows}

    entries: list[CatalogEntry] = []
    for row in sorted(products, key=lambda r: str(r.get("id"))):
        product_id = str(row.get("id"))
        name = str(row.get("name") or "")
        entries.append(
            CatalogEntry(
                product_id=product_id,
                provider_key=row.get("provider_public_key"),
                name=name,
                normalized_name=_compare.fold_text(name),
                tokens=_compare.name_tokens(name),
                price=_price_of(row),
                commission=commission_of_row(commission_by_product.get(product_id)),
                details=tuple(
                    sorted(details_by_product.get(product_id, []),
                           key=lambda pair: (pair[0], pair[1]))
                ),
                conditions=tuple(
                    sorted(conditions_by_product.get(product_id, []),
                           key=lambda c: (c["sort_order"], c["id"]))
                ),
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
            )
        )
    return tuple(entries)


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

EXISTENTE = "existente"
CONFLICTO = "conflicto"
INEXISTENTE = "inexistente"

ACTION_UPDATE = "update"
ACTION_TOUCH = "touch"
ACTION_NEW = "new"

REASON_UNCHANGED = "unchanged"              # same supplier, same offer -> refresh
REASON_REPRICED = "provider_repriced"       # same supplier changed its own offer
REASON_STALE = "stale_yielded"              # catalog row unconfirmed >= 48h
REASON_BETTER_OFFER = "better_offer"        # another supplier, lower price
REASON_COMMISSION_TIE = "commission_tie"    # another supplier, same price, better commission
REASON_CATALOG_BETTER = "catalog_better"    # another supplier, and the catalog still wins
REASON_NO_PRICE = "no_price"                # the offer carries no price to compete with
REASON_IMPLAUSIBLE = "implausible_price"    # the move looks like a parsing accident
REASON_OUTBID = "outbid_in_run"              # another offer of this run took the row
REASON_NO_MATCH = "no_match"

SKIP_CATALOG_KEPT = "CATALOG_OFFER_KEPT"
SKIP_IMPLAUSIBLE = "IMPLAUSIBLE_PRICE"

#: How alike two names must look before a refused pairing is worth mentioning to the
#: owner as "nearly this one". It decides nothing: identity is ``name_identity``.
CATALOG_FLOOR = 0.55


@dataclass(frozen=True)
class Verdict:
    """What one incoming product resolves to, and the reason it landed there."""

    item_key: str
    provider_key: str
    disposition: str
    reason: str
    detail: str
    action: str | None = None
    skip_reason: str | None = None
    product_id: str | None = None
    score: float | None = None
    winner: str = "incoming"
    provider_changes: bool = False
    needs_review: bool = False
    db_name: str | None = None
    db_provider_key: str | None = None
    db_price: float | None = None
    db_commission: dict | None = None
    db_stale_hours: int | None = None
    near_product_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_key": self.item_key,
            "provider_key": self.provider_key,
            "disposition": self.disposition,
            "reason": self.reason,
            "detail": self.detail,
            "action": self.action,
            "skip_reason": self.skip_reason,
            "product_id": self.product_id,
            "score": self.score,
            "winner": self.winner,
            "provider_changes": self.provider_changes,
            "needs_review": self.needs_review,
            "db_name": self.db_name,
            "db_provider_key": self.db_provider_key,
            "db_price": self.db_price,
            "db_commission": self.db_commission,
            "db_stale_hours": self.db_stale_hours,
            "near_product_id": self.near_product_id,
        }


@dataclass(frozen=True)
class Confrontation:
    verdicts: tuple[Verdict, ...]
    funnel: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdicts": [v.as_dict() for v in self.verdicts],
            "funnel": dict(self.funnel),
        }


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def similarity(view: ProductView, entry: CatalogEntry) -> float:
    """Name overlap, on the same scale the inter-supplier step uses.

    Reusing ``compare.ratio`` rather than a fresh implementation is the point: two
    notions of "similar enough" in one pipeline means two different catalogs at the end.
    """
    if not view.tokens or not entry.tokens:
        return 0.0
    return _compare.ratio(view.normalized_name, entry.normalized_name)


#: A token that carries a measurement: a number welded to a unit. This is a *shape*,
#: not a list of properties -- which is what makes it survive a catalog nobody has
#: seen yet.
_SPEC_TOKEN = re.compile(r"^\d+[a-záéíóúñ]{0,4}$")


def _leading_number(token: str) -> str:
    return re.match(r"\d+", token).group(0)  # type: ignore[union-attr]


def spec_tokens(tokens: Sequence[str]) -> frozenset[str]:
    """The measurement-bearing tokens in a name."""
    return frozenset(token for token in tokens if _SPEC_TOKEN.match(token))


def specs_disagree(left: Sequence[str], right: Sequence[str]) -> str | None:
    """Do the two names carry measurements that are not the same one?

    A name-overlap score averages. Two names sharing five of six tokens score far above
    any floor even when the sixth is the one that matters, because one difference that
    decides got averaged away by five that do not.

    So the rule is structural rather than numeric: **when both sides name a measurement
    and the measurements differ, they are not the same product.** No threshold, no
    weighting, nothing to retune.

    One-sided is deliberately *not* a disagreement. R-024 is explicit that an omitted
    property does not imply a different product, and most supplier names carry no
    measurement at all -- treating that as a difference would split every product
    against half its own catalog.
    """
    mine = spec_tokens(left)
    theirs = spec_tokens(right)
    if not mine or not theirs:
        return None
    if mine & theirs:
        return None
    # "110" against "110v" is one measurement written two ways, not two measurements.
    # Compared on the number, so a unit one side left off does not split a product.
    if {_leading_number(token) for token in mine} & {_leading_number(token) for token in theirs}:
        return None
    return f"medida en el nombre: {'/'.join(sorted(mine))} contra {'/'.join(sorted(theirs))}"


def _categorical_clash(view: ProductView, entry: CatalogEntry) -> str | None:
    """The first categorical contradiction between an offer and a catalog entry."""
    for axis in view.comparable_axes:
        if axis.kind != "categorical" or not axis.label:
            continue
        for title, value in entry.details:
            if _compare.fold_text(title) != _compare.fold_text(axis.label):
                continue
            if _compare.contradicts(axis.value, _compare.fold_text(value)):
                return f"{axis.label}: {axis.value} contra {value}"
    return None


def _quantity_clash(view: ProductView, entry: CatalogEntry) -> str | None:
    """A declared quantity that does not agree with the catalog's, in the supplier's units.

    Compared in units, never by a percentage: a watt and a kilometre do not share a
    scale. ``None`` from ``quantities_agree`` means "cannot tell" and is not a clash.
    """
    for axis in view.comparable_axes:
        if axis.kind != "numeric" or axis.quantity is None or not axis.label:
            continue
        for title, value in entry.details:
            if _compare.fold_text(title) != _compare.fold_text(axis.label):
                continue
            other = parse_quantity(value)
            if other is None:
                continue
            if quantities_agree(axis.quantity, other) is False:
                return f"{axis.label}: {axis.value} contra {value}"
    return None


def identity_clash(view: ProductView, entry: CatalogEntry) -> str | None:
    """Why these two are not the same product, or ``None`` when nothing says so."""
    return (
        specs_disagree(view.tokens, entry.tokens)
        or _categorical_clash(view, entry)
        or _quantity_clash(view, entry)
    )


# ---------------------------------------------------------------------------
# Name identity
# ---------------------------------------------------------------------------
#
# A character-overlap score cannot tell a product from its neighbour in the same
# category. Two motorcycles of one brand share most of their letters and differ in the
# only word that matters -- the model. Measured on real offers, an overlap floor of 0.55
# paired a product with a *different* model in roughly one match out of six, each of
# which would have overwritten somebody else's price.
#
# So names are compared as sets of words, and the rule is the same one R-024 states for
# properties: **an omission is not a difference; a contradiction is.**
#
# * If every word of one name appears in the other, the longer name only says more
#   about the same thing ("Kvitova" / "Kvitova Premium"): same product.
# * If each name has a word the other lacks ("Topmaq Tank" / "Topmaq F3"), they
#   contradict: different products.
# * And the names must share at least one word that is not generic to the whole
#   catalog. "Moto Eléctrica" alone identifies nothing.

NAME_NOISE = _compare.NAME_NOISE
identity_words = _compare.identity_words
_pair_words = _compare.pair_words


#: A word is generic when at least this share of all names in play uses it. Derived
#: from the names themselves at run time, so no category or brand list lives here.
GENERIC_SHARE = 0.05

#: Below this share of words in common, a name that merely *contains* a shorter one is
#: not the same product: "Batería Must" is inside the name of an inverter that ships
#: with a Must battery.
IDENTITY_FLOOR = 0.5


def generic_words(names: Sequence[Sequence[str]]) -> frozenset[str]:
    """The words too common across these names to identify a product.

    Counted over *distinct* names: the same product offered by five suppliers is one
    name, and its model word must not become "generic" because it was repeated.
    """
    distinct = {tuple(words) for words in names}
    if not distinct:
        return frozenset()
    counts: dict[str, int] = {}
    for words in distinct:
        for word in set(words):
            counts[word] = counts.get(word, 0) + 1
    floor = max(3, int(len(distinct) * GENERIC_SHARE))
    return frozenset(word for word, count in counts.items() if count >= floor)


def name_identity(left: Sequence[str], right: Sequence[str],
                  generic: frozenset[str]) -> tuple[float | None, str]:
    """``(score, "")`` when the two names name one product, else ``(None, why not)``."""
    shared, only_left, only_right = _pair_words(left, right)
    if not shared:
        return None, "los nombres no comparten ninguna palabra"
    if only_left and only_right:
        return None, (f"cada nombre dice algo que el otro no: "
                      f"{' '.join(only_left)} / {' '.join(only_right)}")
    if not any(a not in generic or b not in generic for a, b in shared):
        return None, "sólo comparten palabras genéricas"
    score = len(shared) / (len(shared) + len(only_left) + len(only_right))
    if score < IDENTITY_FLOOR:
        return None, "un nombre contiene al otro pero dice mucho más; no alcanza para afirmarlo"
    return score, ""


@dataclass(frozen=True)
class Candidate:
    item_key: str
    provider_key: str
    entry: CatalogEntry
    score: float
    price: float | None = None
    commission: dict | None = None

    @property
    def order(self) -> tuple:
        # Best name match first. When two offers of this run name a catalog product
        # equally well they are rivals for it, and R-026 settles rivals: the cheaper
        # one, then the better commission. Then the ids, so the order is total.
        return (
            -self.score,
            float("inf") if self.price is None else float(self.price),
            -_compare.commission_score(self.commission),
            self.item_key,
            self.entry.product_id,
        )


def candidates_for(view: ProductView, entries: Sequence[CatalogEntry],
                   generic: frozenset[str] = frozenset(),
                   ) -> tuple[list[Candidate], tuple[CatalogEntry, str] | None]:
    """Every catalog entry this product may be, plus the nearest one that was refused.

    The refused one is carried so that a product going back to the owner as new can say
    *which* published product it almost was and why it is not -- "no match" and "nearly
    a match, different model" are different things for the person reading it.
    """
    words = identity_words(view.name)
    add_ons = _compare.add_ons_in(view.name)
    accepted: list[Candidate] = []
    refused: tuple[float, CatalogEntry, str] | None = None

    def refuse(closeness: float, entry: CatalogEntry, why: str) -> None:
        nonlocal refused
        if refused is None or closeness > refused[0]:
            refused = (closeness, entry, why)

    for entry in entries:
        score, why = name_identity(words, identity_words(entry.name), generic)
        if score is None:
            closeness = similarity(view, entry)
            if closeness >= CATALOG_FLOOR:
                refuse(closeness, entry, why)
            continue
        # R-025: an add-on announced in one name and not in the other is another product.
        theirs = _compare.add_ons_in(entry.name)
        if add_ons != theirs:
            refuse(score, entry, "uno anuncia un aditamento que el otro no: "
                                 f"{', '.join(sorted(set(add_ons) ^ set(theirs)))}")
            continue
        clash = identity_clash(view, entry)
        if clash is not None:
            refuse(score, entry, clash)
            continue
        accepted.append(Candidate(view.item_key, view.provider_key, entry, score,
                                  view.price, view.commission))
    return accepted, (None if refused is None else (refused[1], refused[2]))


# ---------------------------------------------------------------------------
# R-026 · price first, always
# ---------------------------------------------------------------------------


def better_offer(incoming_price: float | None, incoming_commission: dict | None,
                 entry: CatalogEntry) -> str | None:
    """``'price'`` or ``'commission'`` when the incoming offer wins on money, else ``None``.

    Price decides first and without exception. Commission is consulted **only** when the
    two prices are equal to the cent: a better commission does not pay for a worse
    price. There is no tolerance band here, on purpose.
    """
    if incoming_price is None:
        return None
    if entry.price is None:
        return "price"
    if incoming_price < entry.price:
        return "price"
    if incoming_price > entry.price:
        return None
    if _compare.commission_score(incoming_commission) > _compare.commission_score(entry.commission):
        return "commission"
    return None


def _same_commission(left: dict | None, right: dict | None) -> bool:
    return _compare.commission_score(left) == _compare.commission_score(right)


def _implausible(incoming: float | None, published: float | None) -> bool:
    if incoming is None:
        return False
    if incoming <= 0:
        return True
    if published is None or published <= 0:
        return False
    ratio = incoming / published
    return ratio > IMPLAUSIBLE_PRICE_FACTOR or ratio < 1.0 / IMPLAUSIBLE_PRICE_FACTOR


def _relative_gap(left: float | None, right: float | None) -> float | None:
    """The gap between two prices over the cheaper one, as R-025's examples compute it."""
    if left is None or right is None:
        return None
    low, high = min(left, right), max(left, right)
    if low <= 0:
        return None
    return (high - low) / low


def _money(value: float | None) -> str:
    if value is None:
        return "sin precio"
    return f"{value:g}"


def decide(view: ProductView, entry: CatalogEntry, score: float, now: float | None) -> Verdict:
    """The verdict for one product already shown to be the same as ``entry``."""
    stale_hours = product_stale_hours(entry.created_at, entry.updated_at, now)
    stale = stale_hours is not None and stale_hours >= STALE_HOURS
    same_provider = bool(entry.provider_key) and entry.provider_key == view.provider_key

    base = dict(
        item_key=view.item_key,
        provider_key=view.provider_key,
        product_id=entry.product_id,
        score=round(score, 4),
        db_name=entry.name,
        db_provider_key=entry.provider_key,
        db_price=entry.price,
        db_commission=entry.commission,
        db_stale_hours=stale_hours,
    )

    if same_provider:
        if view.price is None or (
            view.price == entry.price and _same_commission(view.commission, entry.commission)
        ):
            return Verdict(
                disposition=EXISTENTE, reason=REASON_UNCHANGED, action=ACTION_TOUCH,
                winner="catalog",
                detail=f"el mismo proveedor mantiene la oferta ({_money(entry.price)})",
                **base,
            )
        if _implausible(view.price, entry.price):
            return Verdict(
                disposition=CONFLICTO, reason=REASON_IMPLAUSIBLE, action=None,
                skip_reason=SKIP_IMPLAUSIBLE, winner="catalog",
                detail=(f"el precio pasaría de {_money(entry.price)} a {_money(view.price)}: "
                        "parece un error de lectura, no se aplica"),
                **base,
            )
        # A supplier moving its own price is news, and is applied. A supplier moving it
        # by a quarter or more is rare enough that it is far more often two different
        # products under one name; that one waits for the owner.
        own_gap = _relative_gap(view.price, entry.price)
        own_doubt = own_gap is not None and own_gap >= _compare.PRICE_TIEBREAK_REL
        return Verdict(
            disposition=CONFLICTO, reason=REASON_REPRICED, action=ACTION_UPDATE,
            winner="incoming", needs_review=own_doubt,
            detail=(f"el mismo proveedor cambió su oferta: "
                    f"{_money(entry.price)} → {_money(view.price)}"
                    + (f" · la diferencia es del {own_gap:.0%}: confirma que es el mismo "
                       "producto" if own_doubt else "")),
            **base,
        )

    # A different supplier from here on: a competing offer that has to win the row.
    if view.price is None:
        return Verdict(
            disposition=EXISTENTE, reason=REASON_NO_PRICE, action=None,
            skip_reason=SKIP_CATALOG_KEPT, winner="catalog",
            detail="la oferta no trae precio; no puede competir con la publicada",
            **base,
        )

    better = better_offer(view.price, view.commission, entry)
    if not stale and better is None:
        return Verdict(
            disposition=EXISTENTE, reason=REASON_CATALOG_BETTER, action=None,
            skip_reason=SKIP_CATALOG_KEPT, winner="catalog",
            detail=(f"la oferta publicada sigue siendo mejor "
                    f"({_money(entry.price)} contra {_money(view.price)}) y está vigente"),
            **base,
        )

    if _implausible(view.price, entry.price):
        return Verdict(
            disposition=CONFLICTO, reason=REASON_IMPLAUSIBLE, action=None,
            skip_reason=SKIP_IMPLAUSIBLE, winner="catalog",
            detail=(f"el precio pasaría de {_money(entry.price)} a {_money(view.price)}: "
                    "parece un error de lectura, no se aplica"),
            **base,
        )

    if better == "price":
        reason, detail = REASON_BETTER_OFFER, (
            f"otro proveedor lo ofrece más barato: {_money(entry.price)} → {_money(view.price)}")
    elif better == "commission":
        reason, detail = REASON_COMMISSION_TIE, (
            f"mismo precio ({_money(view.price)}) y mejor comisión con otro proveedor")
    else:
        reason, detail = REASON_STALE, (
            f"la oferta publicada lleva {stale_hours} h sin confirmarse; "
            f"entra la vigente: {_money(entry.price)} → {_money(view.price)}")

    # R-025's band, applied where it matters most. Between two suppliers a price gap
    # at or above the band is exactly the case the owner refused to decide by price
    # alone: it may be a markup on the same product or a different product under a
    # similar name. Among incoming offers that question goes to the model. Here the
    # other side is a published product, so it goes to the owner: the change is
    # proposed, and nobody applies it by default.
    gap = _relative_gap(view.price, entry.price)
    doubtful = gap is not None and gap >= _compare.PRICE_TIEBREAK_REL
    if doubtful:
        detail += (f" · la diferencia de precio es del {gap:.0%}: "
                   "confirma que es el mismo producto")

    return Verdict(
        disposition=CONFLICTO, reason=reason, action=ACTION_UPDATE, winner="incoming",
        provider_changes=True, needs_review=doubtful, detail=detail, **base,
    )


# ---------------------------------------------------------------------------
# The step
# ---------------------------------------------------------------------------


def confront(items: Sequence[Mapping[str, Any]], catalog: Sequence[CatalogEntry],
             *, now: float | None = None) -> Confrontation:
    """Confront every incoming product against the catalog.

    ``items`` is what the consolidation left standing: winners and standalones, in the
    shape ``compare.build_view`` reads. Every one of them gets exactly one verdict, and
    no catalog product is the target of more than one.
    """
    views: dict[str, ProductView] = {}
    for record in items:
        view = _compare.build_view(dict(record))
        if view.item_key in views:
            raise ConfrontationRejected(f"item_key repetido en la entrada: {view.item_key}")
        views[view.item_key] = view

    generic = generic_words([identity_words(view.name) for view in views.values()]
                            + [identity_words(entry.name) for entry in catalog])

    pool: list[Candidate] = []
    nearest_refused: dict[str, tuple[CatalogEntry, str]] = {}
    for key in sorted(views):
        accepted, refused = candidates_for(views[key], catalog, generic)
        pool.extend(accepted)
        if refused is not None:
            nearest_refused[key] = refused

    # The assignment. Best pairs first; each side taken once.
    pool.sort(key=lambda candidate: candidate.order)
    taken_products: dict[str, str] = {}
    assigned: dict[str, Candidate] = {}
    lost_to: dict[str, tuple[CatalogEntry, str]] = {}
    for candidate in pool:
        if candidate.item_key in assigned:
            continue
        holder = taken_products.get(candidate.entry.product_id)
        if holder is not None:
            lost_to.setdefault(candidate.item_key, (candidate.entry, holder))
            continue
        assigned[candidate.item_key] = candidate
        taken_products[candidate.entry.product_id] = candidate.item_key

    verdicts: list[Verdict] = []
    for key in sorted(views):
        view = views[key]
        candidate = assigned.get(key)
        if candidate is not None:
            verdicts.append(decide(view, candidate.entry, candidate.score, now))
            continue

        if key in lost_to:
            # The product exists; another offer of this same run won the right to be
            # compared with it. That is not a new product, and not a change either.
            entry, holder = lost_to[key]
            verdicts.append(
                Verdict(
                    item_key=view.item_key, provider_key=view.provider_key,
                    disposition=EXISTENTE, reason=REASON_OUTBID, action=None,
                    skip_reason=SKIP_CATALOG_KEPT, winner="catalog",
                    detail=(f"otra oferta de esta corrida es mejor para «{entry.name}» "
                            f"({holder})"),
                    db_name=entry.name, db_provider_key=entry.provider_key,
                    db_price=entry.price, near_product_id=entry.product_id,
                )
            )
            continue

        near: CatalogEntry | None = None
        if key in nearest_refused:
            near, clash = nearest_refused[key]
            detail = f"parecido a «{near.name}» pero no es el mismo: {clash}"
        else:
            detail = "no hay ningún producto publicado con un nombre parecido"
        verdicts.append(
            Verdict(
                item_key=view.item_key, provider_key=view.provider_key,
                disposition=INEXISTENTE, reason=REASON_NO_MATCH, action=ACTION_NEW,
                winner="incoming", detail=detail,
                near_product_id=None if near is None else near.product_id,
            )
        )

    produced = {verdict.item_key for verdict in verdicts}
    missing = sorted(set(views) - produced)
    if missing or len(verdicts) != len(views):
        raise FunnelUnreconciled(
            f"{len(views)} productos entraron y {len(verdicts)} salieron con veredicto; "
            f"faltan {missing[:8]}"
        )

    targets = [v.product_id for v in verdicts
               if v.product_id and v.action in (ACTION_UPDATE, ACTION_TOUCH)]
    if len(targets) != len(set(targets)):
        raise ConfrontationRejected(
            "dos productos entrantes apuntan al mismo producto publicado; "
            "la asignación tendría que haberlo impedido"
        )

    def count(predicate) -> int:
        return sum(1 for v in verdicts if predicate(v))

    funnel = {
        "incoming": len(views),
        "catalog_size": len(catalog),
        "existente": count(lambda v: v.disposition == EXISTENTE),
        "conflicto": count(lambda v: v.disposition == CONFLICTO),
        "inexistente": count(lambda v: v.disposition == INEXISTENTE),
        "to_update": count(lambda v: v.action == ACTION_UPDATE),
        "to_touch": count(lambda v: v.action == ACTION_TOUCH),
        "to_return": count(lambda v: v.action == ACTION_NEW),
        "no_change": count(lambda v: v.action is None),
        "stale_yields": count(lambda v: v.reason == REASON_STALE),
        "provider_changes": count(lambda v: v.provider_changes),
        "implausible": count(lambda v: v.reason == REASON_IMPLAUSIBLE),
        "needs_review": count(lambda v: v.needs_review),
    }
    funnel["resolved"] = funnel["existente"] + funnel["conflicto"] + funnel["inexistente"]
    if funnel["resolved"] != funnel["incoming"] or (
        funnel["to_update"] + funnel["to_touch"] + funnel["to_return"] + funnel["no_change"]
        != funnel["incoming"]
    ):
        raise FunnelUnreconciled(f"el embudo de la confrontación no cierra: {funnel}")

    return Confrontation(verdicts=tuple(verdicts), funnel=funnel)


__all__ = [
    "STALE_HOURS",
    "RISK_HOURS_CLIENT",
    "RISK_HOURS_OWNER",
    "IMPLAUSIBLE_PRICE_FACTOR",
    "ConfrontationRejected",
    "FunnelUnreconciled",
    "CatalogEntry",
    "Verdict",
    "Confrontation",
    "Candidate",
    "EXISTENTE",
    "CONFLICTO",
    "INEXISTENTE",
    "ACTION_UPDATE",
    "ACTION_TOUCH",
    "ACTION_NEW",
    "CATALOG_FLOOR",
    "product_effective_date",
    "product_stale_hours",
    "is_product_stale",
    "commission_of_row",
    "load_catalog",
    "similarity",
    "identity_words",
    "generic_words",
    "name_identity",
    "specs_disagree",
    "identity_clash",
    "candidates_for",
    "better_offer",
    "decide",
    "confront",
]
