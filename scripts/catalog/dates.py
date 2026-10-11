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

# The second shape a chat arrives in: messages selected and **copied** out of WhatsApp
# and pasted as one block. It is what the owner actually does, because exporting a chat
# is slow and drags the media along. Each message then starts with a bracketed stamp:
#
#     [10/10 9:29 a. m.] +53 5 0000000: caption
#     [9:29 a. m., 10/10/2026] Name: caption        (other phone locales)
#
# The year is usually absent. It is recovered from the run's own reference instant --
# see ``to_datetime`` -- never from today's clock.
COPIED_DATE_FIRST_RE = re.compile(
    r"^\[(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?,?\s+"
    r"(\d{1,2}):(\d{2})(?:\s*([ap])\.?\s*m\.?)?\s*\]\s*",
    re.I,
)
COPIED_TIME_FIRST_RE = re.compile(
    r"^\[(\d{1,2}):(\d{2})(?:\s*([ap])\.?\s*m\.?)?,\s*"
    r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s*\]\s*",
    re.I,
)
_EXPORT_SENDER_RE = re.compile(r"^\s*-\s*[^:]{0,60}:\s?")

#: ``Stamp.year_two_digit`` when the stamp carries no year at all.
NO_YEAR = -1

STYLE_EXPORT = "export"
STYLE_COPIED = "copied"


def _copied(line: str) -> tuple[tuple, str] | None:
    match = COPIED_DATE_FIRST_RE.match(line)
    if match:
        first, second, year, hour, minute, meridiem = match.groups()
    else:
        match = COPIED_TIME_FIRST_RE.match(line)
        if not match:
            return None
        hour, minute, meridiem, first, second, year = match.groups()
    fields = (int(first), int(second), NO_YEAR if year is None else int(year) % 100,
              int(hour), int(minute), (meridiem or "").lower())
    return fields, line[match.end():]


def match_header(line: str) -> tuple[tuple, str, str] | None:
    """Recognise a message header in either shape.

    Returns ``(fields, remainder, style)`` where ``fields`` is
    ``(first, second, year_two_digit, hour, minute, meridiem)`` and ``remainder`` is what
    follows the stamp -- ``" - Sender: caption"`` for an export, ``"Sender: caption"`` for
    a copied block -- or ``None`` when the line starts no message.

    One nesting is unwrapped: an exported chat whose message *is* a pasted block starts
    with an export stamp immediately followed by a copied one. The inner stamp is when
    the supplier announced the product; the outer one is only when somebody forwarded
    it, so the inner one wins.
    """
    copied = _copied(line)
    if copied is not None:
        return copied[0], copied[1], STYLE_COPIED
    match = TIMESTAMP_RE.match(line)
    if not match:
        return None
    remainder = line[match.end():]
    sender = _EXPORT_SENDER_RE.match(remainder)
    if sender:
        inner = _copied(remainder[sender.end():])
        if inner is not None:
            return inner[0], inner[1], STYLE_COPIED
    fields = (int(match.group(1)), int(match.group(2)), int(match.group(3)) % 100,
              int(match.group(4)), int(match.group(5)), (match.group(6) or "").lower())
    return fields, remainder, STYLE_EXPORT


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
    #: How the order was settled when no stamp proved it. Empty when one did.
    fallback: str = ""

    def evidence_payload(self) -> dict:
        """The shape written into each product's payload (R-004's inspectability).

        The proving line is cited, not just summarised, because "why did you read
        1/10/26 as October" is the first question anyone asks of a date filter.
        """
        return {
            "field_order": self.order,
            "rule": self.fallback or (
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
            text = line.rstrip("\n").lstrip("\ufeff")
            header = match_header(text)
            if header is None:
                continue
            first, second, year, hour, minute, meridiem = header[0]
            stamps.append(
                Stamp(
                    raw=text[:120],
                    first=first,
                    second=second,
                    year_two_digit=year,
                    hour=hour,
                    minute=minute,
                    meridiem=meridiem,
                    sender="",
                    line_number=line_number,
                )
            )
    return stamps


def _describe(stamp: Stamp) -> str:
    return f"{stamp.raw!r} (line {stamp.line_number})"


def _fits(stamps: list[Stamp], order: str, reference: datetime | None) -> bool:
    """Is every stamp a real date, not in the future, when read under ``order``?"""
    for stamp in stamps:
        try:
            moment = to_datetime(stamp, order, reference)
        except DateRejected:
            return False
        if reference is not None and moment > reference.replace(tzinfo=None) + _FUTURE_SLACK:
            return False
    return True


def resolve_order(stamps: list[Stamp], reference: datetime | None = None) -> DateOrder:
    """Establish the chat's day/month order, with the evidence for it.

    Proof first: a stamp whose first field exceeds 12 proves day-first, one whose second
    does proves month-first, and a chat that proves both is refused -- two locales were
    merged and every date in it would be a coin flip.

    When nothing proves it -- a pasted block of this week's messages often carries no
    day above 12 -- the order is settled by elimination against ``reference``, the instant
    the run was created: a reading that puts a message in the future, or on a date that
    does not exist, is not a reading. If that leaves one order, it is the answer and the
    payload says why. If it leaves both, the deployment's declared order
    (``limits.DEFAULT_FIELD_ORDER``) is used and the payload says *that*, so a date read
    on a default is never mistaken for one read on proof.
    """
    day_first = [stamp for stamp in stamps if _classify(stamp) == DAY_FIRST]
    month_first = [stamp for stamp in stamps if _classify(stamp) == MONTH_FIRST]

    if day_first and month_first:
        raise DateRejected(
            "this chat proves both field orders, so no single reading is trustworthy: "
            f"day-first at {_describe(day_first[0])}, month-first at "
            f"{_describe(month_first[0])}. It looks like text from two phone locales was "
            "mixed; paste each supplier's messages from one phone"
        )

    unusable = sum(1 for stamp in stamps if stamp.first > 12 and stamp.second > 12)
    ambiguous = sum(1 for stamp in stamps if stamp.first <= 12 and stamp.second <= 12)

    if day_first or month_first:
        return DateOrder(
            order=DAY_FIRST if day_first else MONTH_FIRST,
            proving_stamps=tuple((day_first or month_first)[:5]),
            ambiguous_stamps=ambiguous,
            ignored_stamps=unusable,
        )

    usable = [stamp for stamp in stamps if not (stamp.first > 12 and stamp.second > 12)]
    candidates = [order for order in (DAY_FIRST, MONTH_FIRST)
                  if _fits(usable, order, reference)]
    if not candidates:
        samples = ", ".join(_describe(stamp) for stamp in stamps[:3])
        raise DateRejected(
            f"no reading of these dates is possible: under either field order some "
            f"message is in the future or on a date that does not exist (e.g. {samples})"
        )
    if len(candidates) == 1:
        return DateOrder(
            order=candidates[0], ambiguous_stamps=ambiguous, ignored_stamps=unusable,
            fallback=("no stamp proves the field order; the other reading puts a message "
                      "in the future or on an impossible date"),
        )
    return DateOrder(
        order=limits.DEFAULT_FIELD_ORDER, ambiguous_stamps=ambiguous, ignored_stamps=unusable,
        fallback=("no stamp proves the field order and both readings are possible; "
                  f"read as {limits.DEFAULT_FIELD_ORDER}, the declared format"),
    )


def expand_year(two_digit: int) -> int:
    """Turn WhatsApp's ``26`` into a year without pretending the corpus is 2026 only."""
    year = two_digit + limits.YEAR_CENTURY if two_digit <= limits.YEAR_PIVOT else two_digit + limits.YEAR_PREV_CENTURY
    return year


def _reading(stamp: Stamp, order: str) -> str:
    day, month = (stamp.first, stamp.second) if order == DAY_FIRST else (stamp.second, stamp.first)
    year = "" if stamp.year_two_digit == NO_YEAR else f"/{stamp.year_two_digit}"
    return f"{day}/{month}{year}"


#: A message may be stamped slightly after the run's reference instant (clock skew
#: between phones, a paste made seconds before midnight). Past this it is the future.
_FUTURE_SLACK = timedelta(days=1)


def _apply_meridiem(moment: datetime, meridiem: str) -> datetime:
    # 12:00 a.m. is midnight and 12:00 p.m. is noon, so the hour wraps before the
    # addition rather than after it.
    if meridiem == "p" and moment.hour < 12:
        return moment + timedelta(hours=12)
    if meridiem == "a" and moment.hour == 12:
        return moment - timedelta(hours=12)
    return moment


def to_datetime(stamp: Stamp, order: str, reference: datetime | None = None) -> datetime:
    """Resolve one stamp to a naive local datetime under ``order``.

    Invalid calendar dates raise ``DateRejected`` rather than falling back to a
    default: a stamp of ``31/2`` means the text is damaged or is not a timestamp, and
    guessing February 28 would put a product a day off in a filter whose whole job is to
    be exact.

    A copied stamp has no year. It takes the year of ``reference`` -- the instant the
    run was created, frozen with the run -- or the year before when that would land it
    in the future (a December message pasted in January). Without a reference such a
    stamp cannot be dated and says so; today's clock is never consulted, because a
    replayed slice would then date the same message differently.
    """
    day, month = (stamp.first, stamp.second) if order == DAY_FIRST else (stamp.second, stamp.first)
    if not 1 <= month <= 12:
        raise DateRejected(
            f"{_describe(stamp)} reads as month {month} under {order}, which is not a month; "
            f"the {DAY_FIRST} reading would be {_reading(stamp, DAY_FIRST)} and the "
            f"{MONTH_FIRST} reading {_reading(stamp, MONTH_FIRST)}"
        )

    if stamp.year_two_digit == NO_YEAR:
        if reference is None:
            raise DateRejected(
                f"{_describe(stamp)} carries no year and no reference instant was given "
                "to recover it from"
            )
        local = reference.replace(tzinfo=None)
        last: ValueError | None = None
        for year in (local.year, local.year - 1):
            try:
                moment = _apply_meridiem(
                    datetime(year, month, day, stamp.hour, stamp.minute), stamp.meridiem)
            except ValueError as exc:      # 29/2 outside a leap year
                last = exc
                continue
            if moment <= local + _FUTURE_SLACK:
                return moment
        raise DateRejected(
            f"{_describe(stamp)} is not a date at or before the run's reference under "
            f"{order}" + (f": {last}" if last else "")
        )

    try:
        naive = datetime(expand_year(stamp.year_two_digit), month, day, stamp.hour, stamp.minute)
    except ValueError as exc:
        raise DateRejected(f"{_describe(stamp)} is not a real date under {order}: {exc}") from exc
    return _apply_meridiem(naive, stamp.meridiem)


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


def localize(stamp: Stamp, order: str, zone: ZoneInfo,
             reference: datetime | None = None) -> datetime:
    """The stamp as an aware instant, which is what ``run_items.observed_at`` holds.

    ``reference`` is an aware instant; it is moved into ``zone`` first, because "the
    current year" is a property of the wall clock the messages were written against.
    """
    local = None if reference is None else reference.astimezone(zone)
    return to_datetime(stamp, order, local).replace(tzinfo=zone)