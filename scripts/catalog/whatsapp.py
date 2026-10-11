"""Separating products out of a WhatsApp chat (R-004), with what was kept and what not.

This is a **port**, not an import. the legacy script holds real knowledge about how
these six vendors actually write, and throwing it away to write something prettier
would be a mistake. But the legacy script lives in the private repo, and the runner
that consumes this module is meant to run in the public one, where that import does
not resolve. So the heuristics are re-expressed here with their origin noted, which
is the same move task 5.2 makes with ``commission_rank``.

Kept, because each one was paid for in real corrections:

* Unicode NFKC folding. Vendors send captions in mathematical bold -- ``𝐄𝐒𝐄𝐂𝐈𝐅𝐈𝐂𝐈𝐎𝐍𝐄𝑆`` --
  where ``S`` and ``E`` are astral-plane codepoints. NFKC folds the whole family
  (``strip_font``'s hand-rolled ``FONT_MAP`` never covered the lowercase or digit
  variants).
* Zero-width stripping. WhatsApp interleaves U+200E/200F direction marks inside
  captions and next to attachment names.
* ``PRECIO`` tolerance. Real typos in the corpus: ``PRESIO``, ``pres``, ``price``.
* ``PRICE_CTX``. Variants announce themselves inline -- ``con extensor``,
  ``sin techo``, ``híbrido``, ``de carga`` -- and the comparison stage needs that
  context to separate a cheaper base unit from a more expensive one (D5 rule 1).
* ``pick_name``. The first line of a caption is usually "NUEVO LOTE" or an emoji
  banner, and the product name is two lines down.
* The three-cascade commission extraction, including the inverted
  ``50 de comisión`` phrasing that appears where no number follows the keyword.

Deliberately dropped:

* The folder-name-to-provider-key map and the two hardcoded name-rewriting tables in
  the legacy script. The keys are this customer's real ids and resolve from
  ``runs.sources`` at run time (R-027); the tables are customer data rather than
  judgement and belong on the private runtime channel (R-019 level 4). Their absence
  here is what keeps this module portable to the public repo -- and it is also why the
  nine-char suffix on the corpus's product names survives into step 1 instead of
  being rewritten before a human ever sees it. Fixing that is step 2's job.
* Image-to-product association. The old burst heuristic is real -- photos arrive
  *before* the caption text -- but R-010 keeps images out of the pipeline entirely,
  so an association with no consumer is a liability. The image pattern is still
  applied, for the one job that matters: stripping attachment markers so they cannot
  become the first line of a name.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import dates

IMG_RE = re.compile(r"IMG-\d{8}-WA\d+\.(?:jpg|jpeg|png|webp)", re.I)

# Sender and caption live in different places depending on the header shape, and the
# two shapes have nothing else in common. the legacy script assumed the
# sender-first shape everywhere, which is why its ``MSG_RE`` never matched the
# end-to-end-encryption banner and why a system line would otherwise be read as a
# caption. A sender is capped at 60 characters because the match stops at the first
# colon: without the cap, a system message that happens to contain a colon would hand
# its first sentence over as a "sender" and the rest of the line would be framed as a
# separate message.
HEADER_RE = re.compile(r"^\s*-\s*([^:]{0,60}):(.*)$", re.S)

# Same intent as the legacy parser's name picker. The bar is length: a
# line that is mostly advertising does not become a product name.
PROMO_WORDS = re.compile(
    r"\b(solo|quedan?|disponibles?|nuev[oa]s?|lote|mes|uso|detalles|recien|llegad\w*|"
    r"rebajas?|ofertas?|super|s[uú]per|promo\w*|presio|precio|de)\b", re.I)

PRICE_LINE = re.compile(
    r"(?:presio|precio|pres|price)\s*(?:el[eé]ctrico|hibrido|h[ií]brido|de\s*carga)?"
    r"[a-záéíóúñ\s]*[:\-]?\s*\$?\s*((?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?)\s*(?:usd|cup|eur|\$)?",
    re.I)

PRICE_CTX = re.compile(
    r"(con\s+(?:mensajer[ií]a|extensor|techo|inversor|cabina|panel|goma|expansor)[a-z\sà-ÿ]*|"
    r"sin\s+(?:techo|extensor|mensajer[ií]a|cabina)[a-z\sà-ÿ]*|"
    r"el[eé]ctrico|h[ií]brido|de\s*carga)", re.I)

# "comision", "comición" and the truncated "comion" all occur in real captions.
COMMISSION_LINE = re.compile(r"com[ií](?:s[ií]|c[ií])?[oó]n", re.I)
COMMISSION_FREE = re.compile(r"com(?:is[ií]|i[oó]ci)[oó]n\s*libre", re.I)
# "50 de comisión" reads the number *before* the keyword, which is why the cascades
# are three and not one.
COMMISSION_INVERTED = re.compile(
    r"(\d{1,5})\s*(?:usd|cup|eur|mn|\$)?\s*(?:de\s*)?com[ií](?:s[ií]|c[ií])?[oó]n", re.I)

# A price written with no keyword at all: the amount opens the line and is followed by a
# currency, a parenthesised commission, or both -- "600 USD (10USD COMISIÓN)",
# "55(4 usd comisión)x unidad", "$1200". Consulted only for a caption in which no line
# says "precio", so a caption that does is read exactly as before. The currency or the
# commission is required: a bare number opening a line is a specification, not a price.
BARE_PRICE_LINE = re.compile(
    r"^\s*(?:\$\s*)?((?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d{1,2})?)\s*"
    r"(?:(?:usd|cup|eur|mn)\b|\$|(?=\([^)]*com))",
    re.I)


def price_matches(line: str, bare: bool) -> list[re.Match]:
    """The prices one line states. ``bare`` admits the keyword-less shape."""
    found = list(PRICE_LINE.finditer(line))
    if found or not bare:
        return found
    match = BARE_PRICE_LINE.match(line)
    return [match] if match else []


def is_price_line(line: str, bare: bool) -> bool:
    return bool(price_matches(line, bare))


def uses_bare_prices(text: str) -> bool:
    """True when the caption states its price without ever saying "precio"."""
    return not PRICE_LINE.search(text) and any(
        BARE_PRICE_LINE.match(line) for line in text.splitlines())


def has_price(text: str) -> bool:
    """Does this caption announce something for sale?"""
    return bool(PRICE_LINE.search(text)) or uses_bare_prices(text)

ZERO_WIDTH = str.maketrans("", "", "‎‏‪‫‬‭‮⁦⁧⁨⁩")


def strip_invisible(text: str) -> str:
    """Fold compatibility fonts and drop the direction marks WhatsApp sprinkles in.

    NFKC handles the astral-plane bold alphabets; the translate handles the bidi
    controls that are ordinary ASCII-width and so survive it.
    """
    return unicodedata.normalize("NFKC", text).translate(ZERO_WIDTH).strip()


def clean_name(text: str) -> str:
    """Reduce a caption line to a name: no symbols, no marks, no leftover bullets."""
    folded = strip_invisible(text)
    folded = "".join(ch for ch in folded if unicodedata.category(ch)[0] not in ("S", "M", "C"))
    for junk in ("*", "_", "~", "#", "`", "〽"):
        folded = folded.replace(junk, " ")
    folded = re.sub(r"\s+", " ", folded).strip(" -–—:.")
    return folded


@dataclass
class Message:
    """One WhatsApp message: its stamp, its sender, its caption lines.

    ``line_number`` is the physical line of the message header in the file, and it is
    the second half of the ordering key (R-036). It is captured while walking the
    file rather than recovered afterwards because recovering it means searching the
    message list for a match, and two messages can compare equal -- see
    ``ordinals.py`` for why that matters.
    """

    stamp: dates.Stamp
    lines: list[str] = field(default_factory=list)
    attachment_count: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def sender(self) -> str:
        return self.stamp.sender


def parse_messages(chat_path: Path) -> list[Message]:
    """Walk the chat once, framing messages and keeping each stamp.

    A WhatsApp export is a flat file where a message header is followed by zero or
    more continuation lines. That is the only framing rule there is: a line that
    matches the timestamp starts a message, and anything else belongs to the message
    above it.
    """
    messages: list[Message] = []
    with chat_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.rstrip("\n").lstrip("\ufeff")
            matched = dates.match_header(stripped)
            if matched:
                fields, remainder, style = matched
                if style == dates.STYLE_COPIED:
                    # ``Sender: caption``. The sender of a copied message is a phone
                    # number or a contact name; it is not kept, only split off.
                    sender_end = remainder.find(":")
                    if 0 <= sender_end <= 60:
                        sender, body = remainder[:sender_end].strip(), remainder[sender_end + 1:]
                    else:
                        sender, body = "", remainder
                else:
                    header = HEADER_RE.match(remainder)
                    if header:
                        sender = header.group(1).strip()
                        body = header.group(2)
                    else:
                        # A system notice. It carries a real date and may be the line
                        # that proves the field order, but nobody sent it and it
                        # announces no product.
                        sender = ""
                        body = remainder
                first, second, year, hour, minute, meridiem = fields
                messages.append(
                    Message(
                        stamp=dates.Stamp(
                            raw=stripped[:120],
                            first=first,
                            second=second,
                            year_two_digit=year,
                            hour=hour,
                            minute=minute,
                            meridiem=meridiem,
                            sender=sender,
                            line_number=line_number,
                        )
                    )
                )
            elif messages:
                body = stripped
            else:
                # Before the first message there is only the export banner, which
                # carries a date but announces nothing.
                continue

            body = strip_invisible(body)
            found = list(IMG_RE.finditer(body))
            messages[-1].attachment_count += len(found)
            # Attachment markers must go: an unfound "IMG-20261001-WA0051.jpg" would
            # otherwise sit in the caption and could win pick_name over the real name.
            body = IMG_RE.sub("", body).replace("(archivo adjunto)", "").strip()
            if body:
                messages[-1].lines.append(body)
    return messages


def pick_name(lines: list[str]) -> str:
    """First caption line that names the product rather than advertising or pricing it."""
    bare = uses_bare_prices("\n".join(lines))
    for line in lines:
        if is_price_line(line, bare):
            continue
        candidate = re.sub(r"^[•·\-–—\s]+", "", clean_name(line)).strip()
        if not candidate:
            continue
        stripped = re.sub(r"\s+", " ", PROMO_WORDS.sub(" ", candidate)).strip(" ¡!-–—:.")
        if len(stripped) >= 4:
            return re.sub(r"\bdisponibles?\b", "", candidate, flags=re.I).strip(" -–—:.")
    return clean_name(lines[0]) if lines else ""


def _to_float(raw: str) -> float:
    """Read a Spanish-formatted amount.

    ``54.000`` and ``54,000`` are both thousands separators here; only a separator
    followed by exactly two digits is a decimal point. Getting this backwards turns
    a $5,400 scooter into a $5.40 one, so the rule is stated rather than guessed.
    """
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        return float(raw.replace(".", "").replace(",", ""))
    return float(raw.replace(",", "."))


# The variant phrases that may sit on a line of their own *between* two prices of one
# caption: "Con extensor de rango" above the second "Precio:". Narrower than
# ``PRICE_CTX`` on purpose -- "eléctrico" and "de carga" appear in almost every product
# name, and reading them off a neighbouring line would label every second price.
VARIANT_LINE = re.compile(
    r"\b(con\s+(?:extensor|expansor|techo|cabina|panel|inversor|mensajer[ií]a)[a-z\sà-ÿ]*|"
    r"sin\s+(?:techo|extensor|mensajer[ií]a|cabina)[a-z\sà-ÿ]*)", re.I)


def extract_prices(text: str) -> list[dict]:
    """Every price in the caption, each with its variant context and its own commission.

    The context is read off the price's own line when it is there ("Precio con extensor:
    3950"). For the second and later prices of a caption it may instead sit on a line by
    itself just above ("Con extensor de Rango" / "Precio: 3950"), so the lines since the
    previous price are searched too. The first price never looks back: everything above
    it is the product's own description.

    The commission works the other way round: it is stated on the price's line or on the
    lines right after it, up to the next price. A caption offering "3630, comisión 50"
    and "3580, comisión 25" is offering two deals, and reading the first commission for
    both would publish the cheaper price with the dearer deal's commission.
    """
    bare = uses_bare_prices(text)
    prices: list[dict] = []
    since_last: list[str] = []
    for line in text.splitlines():
        matches = price_matches(line, bare)
        if not matches:
            since_last.append(line)
            if prices:
                prices[-1]["_after"].append(line)
            continue
        for match in matches:
            context_match = PRICE_CTX.search(line)
            context = context_match.group(0).strip().lower() if context_match else ""
            if not context and prices:
                for earlier in reversed(since_last):
                    found = VARIANT_LINE.search(earlier)
                    if found:
                        context = found.group(0).strip().lower()
                        break
            prices.append({"value": _to_float(match.group(1)), "raw": line.strip(),
                           "context": context, "_after": [line[match.end():]]})
        since_last = []
    for price in prices:
        price["commission"] = extract_commission("\n".join(price.pop("_after")))
    return prices


def extract_commission(text: str) -> dict | None:
    """Commission, free commission, or nothing.

    Three cascades because the corpus uses three phrasings and only the first two are
    keyword-first: ``COMISIÓN: 30 USD``, ``comisión libre``, and ``50 de comisión``.
    """
    for line in text.splitlines():
        match = COMMISSION_LINE.search(line)
        if not match:
            continue
        if COMMISSION_FREE.search(line):
            return {"free": True, "source": "keyword-free"}
        tail = re.sub(r"[^\d.,]", " ", line[match.end():])
        amount = re.search(r"(\d{1,5}(?:[.,]\d{1,2})?)", tail)
        if amount:
            return {"value": _to_float(amount.group(1)), "source": "keyword-first"}
    inverted = COMMISSION_INVERTED.search(text)
    if inverted:
        return {"value": float(inverted.group(1)), "source": "inverted"}
    free = COMMISSION_FREE.search(text)
    if free:
        return {"free": True, "source": "keyword-free"}
    return None


# R-027 classifies a field as provider-scoped (never transferable to another
# provider's product) or generic. Only the provider-scoped half is needed here, and
# it is exactly the vocabulary PRICE_CTX already carries: an add-on is sold by the
# provider who listed it and must not migrate onto a winner from a different vendor.
PROVIDER_SCOPED_CONTEXT = ("con ", "sin ", "extensor", "techo", "mensajería", "mensajeria",
                           "cabina", "panel", "goma", "inversor")


def scope_for(context: str) -> str:
    """R-027's provider-scoped half, read off the add-on vocabulary PRICE_CTX carries."""
    return "provider_scoped" if any(token in context for token in PROVIDER_SCOPED_CONTEXT) else "generic"