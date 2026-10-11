"""Turning "the Nth thing in this chat" into an identity that survives a kill (R-036).

``run_items`` is keyed on ``(run_id, item_key)`` and the stamp that makes replay safe
(R-031) hangs off that key. So the key has to mean the same product in every process
that ever touches the run: a slice killed mid-comparison, re-dispatched twenty minutes
later, and re-deriving its ordinals has to land on the same rows.

The trap is specific and it is Python's, not SQL's. CPython salts string hashing per
process unless ``PYTHONHASHSEED`` says otherwise, so two runs of identical code over
an identical file see a ``set`` of the same strings in different orders. Iterate that
set to number your products and every number moves. Nothing errors. The run resumes
against a shifted index and re-decides the wrong products -- the worst class of bug in
this pipeline, because the catalog that comes out still looks like a catalog.

So the order is declared rather than inherited. Three primitives, all read off a file
R-036 freezes, in a strict priority:

1. ``observed_at`` -- when the product was announced.
2. ``line_number_in_chat`` -- where in the file that announcement sits.
3. ``variant_index`` -- which price variant of that one message this is.

The third is not decoration. A caption with four prices becomes four products, all
four sharing a timestamp and a line, so the first two components tie and the order is
not total. Leaving that tie to sort stability would make the numbering depend on the
order products happened to be built in -- deterministic in practice, but only because
of an implementation detail three modules away. Declaring the tiebreak makes the
ordering total by construction, which is what R-036 actually asks for.

The assertion at the end is the belt to that braces: if two products ever share a full
key, the numbering is ambiguous and the run stops instead of quietly assigning the same
ordinal twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import limits

ITEM_KEY_SEPARATOR = "-"


@dataclass
class Ordered:
    """A product awaiting its ordinal, carrying the three ordering primitives."""

    provider_key: str
    observed_at: datetime
    line_number_in_chat: int
    variant_index: int
    label: str = ""
    item_key: str = ""
    ordinal: int = 0

    @property
    def sort_key(self) -> tuple[datetime, int, int]:
        return (self.observed_at, self.line_number_in_chat, self.variant_index)


def ordinal_digits(count: int) -> int:
    """Width of the ordinal field, never below the three digits the CHECK demands."""
    width = len(str(max(count, 1)))
    return min(max(width, limits.ORDINAL_MIN_DIGITS), limits.ORDINAL_MAX_DIGITS)


def make_item_key(provider_key: str, ordinal: int, digits: int) -> str:
    """``<provider_key>-<NNN>``.

    Matches ``run_items_item_key_check``: ``^[A-Za-z0-9_-]+-[0-9]{3,6}$``. The
    separator is safe inside a provider key that already contains underscores because
    the numeric tail is what makes the split unambiguous.
    """
    return f"{provider_key}{ITEM_KEY_SEPARATOR}{ordinal:0{digits}d}"


def assign(items: list[Ordered]) -> list[Ordered]:
    """Number ``items`` by their total key and stamp each one with its ``item_key``.

    ``sort`` on a tuple of primitives is stable and defined for every input: two
    datetimes compare by instant, two ints compare by magnitude, and there is no
    hash-ordered structure anywhere in the path. Returns a new list; the caller's is
    left alone so the parse order and the ordinal order can be compared.
    """
    ordered = sorted(items, key=lambda item: item.sort_key)

    seen: dict[tuple[datetime, int, int], str] = {}
    for item in ordered:
        key = item.sort_key
        if key in seen:
            raise ValueError(
                "two products share the ordering key "
                f"(observed_at={item.observed_at.isoformat()}, "
                f"line={item.line_number_in_chat}, variant={item.variant_index}) so their "
                f"ordinals cannot be told apart; the first was {seen[key]}"
            )
        seen[key] = item.label or str(key)

    digits = ordinal_digits(len(ordered))
    for position, item in enumerate(ordered, start=1):
        item.ordinal = position
        item.item_key = make_item_key(item.provider_key, position, digits)
    return ordered