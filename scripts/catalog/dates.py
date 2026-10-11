"""WhatsApp timestamps: capture them, then prove how to read them (R-004, G-02).

the legacy script matches the timestamp and throws it away -- the capture group
covers the sender and nothing else. So the repo has zero date code and ``observed_at``
does not exist anywhere. This module supplies both halves: a regular expression that
keeps the stamp, and an order-resolution pass that decides whether the stamp reads
day-first or month-first *with evidence attached*.

Why not decide per message
---------------------------
G-02 asks for validation rather than inference, and asks that a date which fits
neither reading fail loudly. Read per-message, that rule kills the corpus: measured
over all six chats, 649 of 655 message timestamps have both fields at or under 12 and
are therefore ambiguous in isolation. Only six lines prove anything at all, one per
chat, and every one of them is WhatsApp's own "messages are end-to-end encrypted"
banner -- the first line of every export.

The format is a property of the export, not of the line. One WhatsApp client with one
locale setting wrote the whole file, so the right resolution unit is the chat: scan
every stamp, collect what each one *proves*, and apply the conclusion to the file.
That is strictly more rigorous than a per-line guess -- the answer is derived from
proof present in the data, with the proving line cited -- and it is the only reading
under which the corpus is ingestible at all.

What still fails loudly
-----------------------
* Both orders proven somewhere in the same chat. A uniform locale cannot produce
  this; it means a merged or hand-edited export, and every product's date would be a
  coin flip.
* No proof anywhere. Every stamp ambiguous, typically a chat shorter than the first
  twelve days of a month. The message names both candidate readings instead of
  picking one, which is the spec's scenario with a resolution unit that can actually
  fire.
* A stamp that is not a real calendar date under the proven order, such as ``5/13/26``
  read day-first. Proof of the order does not make every stamp valid.

Timezone
--------
A WhatsApp stamp is local wall clock with no zone. ``observed_at`` is ``timestamptz``.
The two are reconciled here, with ``runs.effective_tz``, so that R-021's strict filter
compares instants and not two different ideas of "midnight".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import limits

# The capture group that the legacy script lacks: the stamp is split into its five
# numeric parts plus meridiem, so nothing has to be re-parsed from the matched string
# later.
#
# Deliberately stops after the time. WhatsApp emits two shapes for a message header
# -- ``1/10/26, 8:43 p. m. - Sender: caption`` and ``24/9/26, 2:10 a. m. - this chat
# is end-to-end encrypted`` -- and the second has no sender at all. Splitting sender
# from caption is therefore not this layer's job (see ``whatsapp.HEADER_RE``), and a
# greedy trailing group here would swallow the caption on every real message. Keeping
# the two concerns apart is also what lets the end-to-end-encryption banner count as
# date evidence: it is the only line in the corpus that proves the field order.
TIMESTAMP_RE = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{2,4}),\s*"
    r"(\d{1,2}):(\d{2})(?:\s*([ap])\.?\s*m\.?)?"
    r"(?=\s|$)",
    re.I,
)

DAY_FIRST = "d/m"
MONTH_FIRST = "m/d"


class DateRejected(Exception):
    """A chat's date format cannot be established from the chat itself.

    The message always carries the raw stamps and both readings, so the failure is
    actionable without opening the file.
    """


@dataclass(frozen=True)
class Stamp:
    """One matched timestamp, still unresolved."""

    raw: str
    first: int
    second: int
    year_two_digit: int
    hour: int
    minute: int
    meridiem: str
    sender: str
    line_number: int

    @property
    def evidence(self) -> str:
        if self.first > 12:
            return f"field 1 ({self.first}) exceeds 12, so it cannot be a month"
        if self.second > 12:
            return f"field 2 ({self.second}) exceeds 12, so it cannot be a day"
        return "both fields are 12 or under; this stamp alone cannot order itself"


@dataclass(frozen=True)
class DateOrder:
    """The chat's proven field order, with the lines that proved it."""

    order: str
    proving_stamps: tuple[Stamp, ...] = field(default=())
    ambiguous_stamps: int = 0
    ignored_stamps: int = 0

    def evidence_payload(self) -> dict:
        """The shape written into each product's payload (R-004's inspectability).

        The proving line is cited, not just summarised, because "why did you read
        1/10/26 as October" is the first question anyone asks of a date filter.
        """
        return {
            "field_order": self.order,
            "rule": (
                "first field exceeds 12, so the chat is day-first"
                if self.order == DAY_FIRST
                else "second field exceeds 12, so the chat is month-first"
            ),
            "proof": [
                {
                    "stamp": stamp.raw,
                    "line_number": stamp.line_number,
                    "reason": stamp.evidence,
                }
                for stamp in self.proving_stamps
            ],
            "ambiguous_stamps_resolved_by_chat_order": self.ambiguous_stamps,
        }


def _classify(stamp: Stamp) -> str:
    """What this one stamp can prove: ``d/m``, ``m/d``, or nothing."""
    if stamp.first > 12 and stamp.second <= 12:
        return DAY_FIRST
    if stamp.second > 12 and stamp.first <= 12:
        return MONTH_FIRST
    if stamp.first > 12 and stamp.second > 12:
        return ""  # neither field can be a month; unusable as evidence
    return ""


def scan_stamps(chat_path: Path) -> list[Stamp]:
    """Every timestamp in the chat, in file order, with its line number.

    Lines that do not match are continuations of the previous message or WhatsApp's
    export header; they carry no date and are not evidence either way.
    """
    stamps: list[Stamp] = []
    with chat_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            match = TIMESTAMP_RE.match(line.rstrip("\n"))
            if not match:
                continue
            stamps.append(
                Stamp(
                    raw=line.rstrip("\n")[:120],
                    first=int(match.group(1)),
                    second=int(match.group(2)),
                    year_two_digit=int(match.group(3)),
                    hour=int(match.group(4)),
                    minute=int(match.group(5)),
                    meridiem=(match.group(6) or "").lower(),
                    sender="",
                    line_number=line_number,
                )
            )
    return stamps


def _describe(stamp: Stamp) -> str:
    return f"{stamp.raw!r} (line {stamp.line_number})"


def resolve_order(stamps: list[Stamp]) -> DateOrder:
    """Establish the chat's day/month order, or refuse to.

    ``stamps`` must be non-empty: a chat with no parseable timestamp has no products
    either, and the caller hits that before reaching here.
    """
    day_first = [stamp for stamp in stamps if _classify(stamp) == DAY_FIRST]
    month_first = [stamp for stamp in stamps if _classify(stamp) == MONTH_FIRST]

    if day_first and month_first:
        raise DateRejected(
            "this chat proves both field orders, so no single reading is trustworthy: "
            f"day-first at {_describe(day_first[0])}, month-first at "
            f"{_describe(month_first[0])}. It looks like exports from two locales were "
            "merged; re-export the chat as one file"
        )

    unusable = sum(1 for stamp in stamps if stamp.first > 12 and stamp.second > 12)
    if not day_first and not month_first:
        samples = ", ".join(_describe(stamp) for stamp in stamps[:3])
        raise DateRejected(
            f"no timestamp in this chat settles the field order; all {len(stamps)} are "
            f"ambiguous (e.g. {samples}). Both readings are open: {DAY_FIRST} would read "
            f"{_reading(stamps[0], DAY_FIRST)} and {MONTH_FIRST} would read "
            f"{_reading(stamps[0], MONTH_FIRST)}. Refusing to pick"
        )

    order = DAY_FIRST if day_first else MONTH_FIRST
    proof = (day_first or month_first)[:5]
    ambiguous = sum(1 for stamp in stamps if stamp.first <= 12 and stamp.second <= 12)
    return DateOrder(
        order=order,
        proving_stamps=tuple(proof),
        ambiguous_stamps=ambiguous,
        ignored_stamps=unusable,
    )


def expand_year(two_digit: int) -> int:
    """Turn WhatsApp's ``26`` into a year without pretending the corpus is 2026 only."""
    year = two_digit + limits.YEAR_CENTURY if two_digit <= limits.YEAR_PIVOT else two_digit + limits.YEAR_PREV_CENTURY
    return year


def _reading(stamp: Stamp, order: str) -> str:
    day, month = (stamp.first, stamp.second) if order == DAY_FIRST else (stamp.second, stamp.first)
    return f"{day}/{month}/{stamp.year_two_digit}"


def to_datetime(stamp: Stamp, order: str) -> datetime:
    """Resolve one stamp to a naive local datetime under ``order``.

    Invalid calendar dates raise ``DateRejected`` rather than falling back to a
    default: a stamp of ``31/2/26`` means the export is damaged or the text is not a
    timestamp, and guessing February 28 would put a product a day off in a filter
    whose whole job is to be exact.
    """
    day, month = (stamp.first, stamp.second) if order == DAY_FIRST else (stamp.second, stamp.first)
    if not 1 <= month <= 12:
        raise DateRejected(
            f"{_describe(stamp)} reads as month {month} under {order}, which is not a month; "
            f"the {DAY_FIRST} reading would be {_reading(stamp, DAY_FIRST)} and the "
            f"{MONTH_FIRST} reading {_reading(stamp, MONTH_FIRST)}"
        )
    try:
        naive = datetime(expand_year(stamp.year_two_digit), month, day, stamp.hour, stamp.minute)
    except ValueError as exc:
        raise DateRejected(f"{_describe(stamp)} is not a real date under {order}: {exc}") from exc

    if stamp.meridiem == "p":
        # 12:00 a.m. is midnight and 12:00 p.m. is noon, so the hour wraps before the
        # addition rather than after it.
        if naive.hour < 12:
            naive += timedelta(hours=12)
    elif stamp.meridiem == "a":
        if naive.hour == 12:
            naive -= timedelta(hours=12)
    return naive


def load_zone(name: str) -> ZoneInfo:
    """Resolve ``runs.effective_tz``, refusing an unknown zone rather than falling back.

    Silently defaulting to UTC here would move every product in the chat by four or
    five hours, and the resulting catalog would still look entirely plausible.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DateRejected(
            f"effective_tz {name!r} is not a known IANA zone; supply one the runner's tz "
            "database actually has"
        ) from exc


def localize(stamp: Stamp, order: str, zone: ZoneInfo) -> datetime:
    """The stamp as an aware instant, which is what ``run_items.observed_at`` holds."""
    return to_datetime(stamp, order).replace(tzinfo=zone)