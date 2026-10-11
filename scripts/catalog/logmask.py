"""Get the identifiers out of the log before they get out of the log.

A run's ``item_key`` is built from the provider's real key, and the log of a public repository is readable without signing in. So every
identifier that can reach stdout has to be registered as a GitHub log mask
*before* it is printed, not scrubbed afterwards.

Why masking rather than not printing
-------------------------------------
``item_key`` is how a verdict refers to itself, so a progress report without it
is a report nobody can act on. The alternative —print indexes and hope the owner
reconstructs which item is which — turns a diagnosable run into a guessing game
at exactly the moment something went wrong. So the identifiers stay; what changes
is that the log never holds them.

``register_mask`` also covers the derived values, not only the raw keys: a
provider name in a catalog snapshot, a phone number out of a product's
conditions. Masking the phone number matters more than the run id, because that
one belongs to a person and the run id does not.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: Values shorter than this are not worth masking: they would match everything.
#: A three-character "id" appears in ordinary prose and would turn the log to
#: asterisks without protecting anything.
MIN_MASK_LENGTH = 6

#: The shapes that are worth catching wherever they appear, whether or not
#: something remembered them: a provider key, a UUID, an email, a phone.
_PATTERNS = (
    re.compile(r"PRV_[A-Za-z0-9]{4,}"),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    # Phone numbers last, on purpose: this is the one shape that belongs to a
    # person rather than to a run, and it reaches stdout through product
    # conditions and supplier text. Seven to fifteen digits with the usual
    # separators, which covers both local and international forms.
    re.compile(r"\+?\d[\d\s().-]{6,17}\d"),
)

_seen: set[str] = set()


def register_mask(value: Any) -> bool:
    """Tell GitHub to replace ``value`` wherever it appears in the log.

    Returns whether it was registered. Idempotent, because a value registered
    twice costs nothing and a value registered once by accident is a value that
    appears unmasked somewhere later.
    """
    text = "" if value is None else str(value).strip()
    if len(text) < MIN_MASK_LENGTH or text in _seen:
        return False
    _seen.add(text)
    print(f"::add-mask::{text}")
    return True


def register_all(values: Iterable[Any]) -> list[str]:
    """Register a batch; returns the ones that actually went in."""
    return [text for text in map(str, values) if register_mask(text)]


def scrub(text: str) -> str:
    """Redact the shapes in ``text`` for the cases masking cannot cover.

    A mask is registered *after* a line is already written — GitHub replaces it
    in the stored log, but a value that never got registered leaks. So anything
    going to a place masks do not reach (an exception message that leaves the
    machine, a summary that gets pasted into a chat) goes through this instead.
    """
    out = text
    for pattern in _PATTERNS:
        out = pattern.sub(lambda m: m.group(0)[:2] + "***", out)
    return out


__all__ = [
    "MIN_MASK_LENGTH",
    "register_mask",
    "register_all",
    "scrub",
]