"""Step 1: one list, one shape, every field dated and attributed (R-004, R-022, R-027).

This is the stage that runs with no credentials (§7.1), so it cannot write and does
not try. It produces a deterministic list of records that a later stage with
credentials validates and persists.

Three obligations, and they are separate from each other:

**One shape regardless of input count (R-022).** A run over one chat and a run over
four must emit records that differ only in count and in the value of ``provider_key``.
That is enforced by a signature -- the sorted set of ``(field_name, json_type)`` pairs
over a record, hashed -- computed per record and compared across the run. A field that
appears in one record and not another fails the step and names itself, rather than
producing a list whose shape drifts with the input.

**Every product dated by the message that announced it (R-004).** ``observed_at``
comes from the announcing message's stamp, resolved through ``dates.py``. It is never
today's clock and never the file's mtime, both of which would make a re-run produce a
different ordinal order.

**Every extracted value attributed before anything merges (R-027).** The tag is
created here because a merge is the operation it has to constrain; tagging at merge
time would be tagging the output of the thing that needed constraining. Each price,
each caption-derived line and the commission carries ``source_provider`` and a scope,
and an untagged or foreign-tagged field fails the run.

Compatibility with the comparison stage is deliberate: ``name``, ``price``,
``commission`` and ``text`` keep the names and shapes the legacy converter
already reads, and ``item_key`` replaces the ``id_aux`` that stage invents by
numbering an unordered list. ``images`` is *not* carried -- R-010 forbids images in
every artifact, so leaving the field in place with an empty list would only invite a
later stage to write into it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__:
    from . import dates, limits, ordinals, sources, whatsapp
else:
    # Invoked as a plain script, which is how the runner and the corpus check call
    # it. ``sys.path[0]`` is then this directory rather than its parent, so the
    # package is not importable until the parent is added.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from catalog import dates, limits, ordinals, sources, whatsapp

# The declared shape of an ingested record, as (name, json type). It is written out
# rather than inferred so that a field nobody intended to add fails the step instead
# of quietly widening the signature.
CANONICAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("item_key", "string"),
    ("ordinal", "integer"),
    ("provider_key", "string"),
    ("observed_at", "string"),
    ("parsed_date", "string"),
    ("name", "string"),
    ("price", "number"),
    ("commission", "object"),
    ("prices", "array"),
    ("spec_lines", "array"),
    ("date_evidence", "object"),
    ("text", "string"),
    ("shape_signature", "string"),
)

# Fields whose type admits null. ``commission`` is absent from most captions, and a
# run where most records are null and some are objects is one shape, not two.
NULLABLE_FIELDS = frozenset({"commission", "price"})

# Inside a record, only these carry extracted values. Identity and evidence fields
# are not extracted data: they say which product this is, not what the vendor said.
PROVENANCE_BEARING = ("prices", "spec_lines")


class IngestRejected(Exception):
    """Step 1 cannot produce a list it is willing to hand downstream."""


def json_type(value) -> str:
    """The type name a record field is declared as, in the JSON vocabulary."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise IngestRejected(f"value of type {type(value).__name__} has no JSON type")


def effective_shape(record: dict) -> list[tuple[str, str]]:
    """The record's ``(field_name, json_type)`` pairs, sorted by name.

    A null in a nullable slot reports as that slot's declared type rather than as
    ``null``, because "no commission was announced" and "a free commission was
    announced" describe the same shape. Without this, the first caption without a
    commission would change the run's signature and R-022's single-shape guarantee
    would be a property of the vendors' punctuation rather than of the pipeline.

    A null anywhere else still reports as ``null`` and fails ``check_shape``, so this
    collapses only the emptiness it is allowed to collapse.
    """
    declared = dict(CANONICAL_FIELDS)
    pairs = []
    for name in sorted(record):
        kind = json_type(record[name])
        if kind == "null" and name in NULLABLE_FIELDS:
            kind = declared[name]
        pairs.append((name, kind))
    return pairs


def shape_signature(record: dict) -> str:
    """Hash the record's field names and types, sorted, order-independent.

    Values are excluded on purpose. Two records from two different vendors have
    different prices and different names and must still land on one signature -- that
    is exactly the "one chat versus four chats" scenario.
    """
    canonical = "\n".join(f"{name}:{kind}" for name, kind in effective_shape(record))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def check_shape(record: dict, expected: str | None = None) -> str:
    """Validate one record against the declared shape and return its signature.

    Both directions matter. A record carrying a field the shape does not declare is
    the ad-hoc-field scenario from the spec and fails by name; a record *missing* one
    is the same defect wearing a disguise, and would otherwise be what lets a run's
    signature drift from one source to the next.
    """
    declared = dict(CANONICAL_FIELDS)
    actual = {name: json_type(value) for name, value in record.items()}

    # Written as a comprehension over two dicts rather than a set difference so that
    # no set is built or iterated here. The result feeds an error message, so its order
    # has to be stable, and a grep for hash-ordered iteration across the package should
    # come back empty rather than needing an argument about a wrapping `sorted()`.
    extra = sorted(name for name in actual if name not in declared)
    if extra:
        raise IngestRejected(
            f"record carries field(s) {extra} that the canonical shape does not declare; "
            "step 1 refuses to emit a heterogeneous list"
        )
    for name, kind in declared.items():
        if name not in record:
            raise IngestRejected(f"record is missing declared field {name!r}")
        if name in NULLABLE_FIELDS and record[name] is None:
            continue
        if actual[name] != kind:
            raise IngestRejected(
                f"field {name!r} is {actual[name]}, canonical shape declares {kind}"
            )

    signature = shape_signature(record)
    if expected is not None and signature != expected:
        raise IngestRejected(
            "record shape differs from the rest of the run: "
            f"{sorted(actual.items())} vs the run's signature {expected}"
        )
    return signature


def assert_provenance(record: dict) -> None:
    """R-027's gate: no extracted value without provenance, none from a foreign vendor.

    A field with a null ``source_provider`` would merge freely into whichever product
    wins comparison, which is precisely the leak R-027 exists to stop. A field tagged
    with someone else's key means a product already carries another vendor's
    commercial terms, and that is the mistake the owner called out by name: we do not
    want to mislead the client.
    """
    provider_key = record["provider_key"]
    for bucket in PROVENANCE_BEARING:
        for position, entry in enumerate(record.get(bucket) or []):
            owner = entry.get("source_provider")
            if owner is None:
                raise IngestRejected(
                    f"item {record['item_key']}: {bucket}[{position}] "
                    f"({entry.get('label') or entry.get('text') or 'unnamed'}) has no "
                    "source_provider; an unattributed field may not exist"
                )
            if owner != provider_key:
                raise IngestRejected(
                    f"item {record['item_key']}: {bucket}[{position}] carries "
                    f"source_provider={owner!r} but the item belongs to {provider_key!r}; "
                    "a provider-scoped field may not cross vendors"
                )
    commission = record.get("commission")
    if commission is not None and commission.get("source_provider") != provider_key:
        raise IngestRejected(
            f"item {record['item_key']}: commission carries "
            f"source_provider={commission.get('source_provider')!r}, expected {provider_key!r}"
        )


@dataclass
class SourceSpec:
    """One declared source: a provider key and the uploaded file behind it.

    ``provider_key`` is supplied by the caller and resolves from ``runs.sources``.
    There is no table in this repo mapping folder names to keys, and that absence is
    the point (R-027): the legacy parser embedded six real customer ids in a map keyed
    by directory name, which is what makes shipping the runner to a public repo
    unworkable.
    """

    provider_key: str
    path: Path
    label: str = ""


@dataclass
class SourceReport:
    """What happened to one source, for the operator and for the run's event log."""

    provider_key: str
    declared_bytes: int
    entry_count: int
    origin: str
    field_order: str
    proving_stamp: str = ""
    ambiguous_stamps: int = 0
    messages: int = 0
    products: int = 0
    notes: list[str] = field(default_factory=list)


def _spec_lines(text: str, commission: dict | None) -> list[dict]:
    """Caption lines that are neither a price nor the commission.

    Kept raw on purpose. Deciding which line is ``Potencia`` and which is
    ``Garantía`` is a catalogue question and belongs to the stage that has the
    catalogue loaded; splitting it here would mean embedding one.
    """
    out: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if whatsapp.PRICE_LINE.search(line):
            continue
        if commission and whatsapp.COMMISSION_LINE.search(line):
            continue
        cleaned = whatsapp.clean_name(stripped)
        if len(cleaned) < 3:
            continue
        out.append(
            {
                "text": stripped,
                "label": cleaned.split(":")[0][:60] if ":" in cleaned else "",
                "scope": "generic",
            }
        )
    return out


def _expand_variants(message: whatsapp.Message, provider_key: str) -> list[dict]:
    """One dict per sellable thing in this caption.

    A caption with a single price yields one variant. A caption with several yields
    one each, and the variant's ``PRICE_CTX`` context decides whether the caption name
    gets an add-on suffix -- the same rule the legacy script applied, because
    "con extensor de rango" and the bare unit are not the same product and price
    comparison must be able to tell them apart.
    """
    text = message.text
    prices = whatsapp.extract_prices(text)
    if not prices:
        return []

    base_name = whatsapp.pick_name(message.lines)
    commission = whatsapp.extract_commission(text)
    variants: list[dict] = []

    if len(prices) == 1 or len({price["value"] for price in prices}) == 1:
        # A single distinct price repeated across lines is one offer described twice.
        chosen = prices[0]
        variants.append(
            {
                "name": base_name,
                "price": chosen["value"],
                "prices": [_tagged_price(chosen, provider_key)],
                "commission": commission,
            }
        )
        return variants

    has_extensor = any("extensor" in price["context"] for price in prices)
    has_hibrido = any("hibrido" in price["context"] or "híbrido" in price["context"]
                      for price in prices)
    for price in prices:
        suffix = ""
        if has_extensor and "extensor" in price["context"]:
            suffix = " con Extensor de Rango"
        elif has_hibrido and ("hibrido" in price["context"] or "híbrido" in price["context"]):
            suffix = " (Híbrido)"
        variants.append(
            {
                "name": f"{base_name}{suffix}" if suffix else base_name,
                "price": price["value"],
                "prices": [_tagged_price(price, provider_key)],
                "commission": commission,
            }
        )
    return variants


def _tagged_price(price: dict, provider_key: str) -> dict:
    """Attach provenance and scope to one price before anything can merge it."""
    return {
        "value": price["value"],
        "context": price["context"],
        "scope": whatsapp.scope_for(price["context"]),
        "source_provider": provider_key,
    }


def ingest_source(
    spec: SourceSpec,
    slot: int,
    work_dir: Path,
    zone: ZoneInfo,
) -> tuple[list[dict], SourceReport]:
    """Ingest one chat into records carrying their ordinals, signatures and tags.

    Two passes over the same file, on purpose. The first proves the field order from
    every stamp in the chat; the second then reads that order and dates each product.
    Resolving the order lazily -- message by message -- would mean either failing on
    the first ambiguous stamp, which is 649 of 655 in this corpus, or picking an
    order the first time one happens to be provable, which is a guess wearing a
    costume.
    """
    chat = sources.materialize_chat(spec.path, slot, work_dir, spec.label or spec.provider_key)

    stamps = dates.scan_stamps(chat.path)
    if not stamps:
        raise IngestRejected(
            f"source {spec.provider_key}: no WhatsApp timestamp found in "
            f"{chat.declared_bytes} bytes; the upload is not a chat export"
        )
    order = dates.resolve_order(stamps)

    messages = whatsapp.parse_messages(chat.path)
    # The ordinal primitive and the record are built together and travel as a pair:
    # the ordering decision and the payload must describe the same product, so they
    # are not allowed to drift apart and be re-associated afterwards.
    paired: list[tuple[ordinals.Ordered, dict]] = []

    for message in messages:
        if not message.text.strip():
            # An attachment-only message announces nothing. It is not a product and
            # not a variant of one, so it does not consume a variant index either.
            continue
        if not whatsapp.PRICE_LINE.search(message.text):
            continue
        observed_at = dates.localize(message.stamp, order.order, zone)
        for variant_index, variant in enumerate(_expand_variants(message, spec.provider_key)):
            paired.append(
                (
                    ordinals.Ordered(
                        provider_key=spec.provider_key,
                        observed_at=observed_at,
                        line_number_in_chat=message.stamp.line_number,
                        variant_index=variant_index,
                        label=variant["name"],
                    ),
                    {
                        "variant": variant,
                        "observed_at": observed_at,
                        "text": message.text,
                        "sender": message.sender,
                    },
                )
            )

    ordered = ordinals.assign([item for item, _ in paired])
    by_key = {item.sort_key: item for item in ordered}

    signature: str | None = None
    final: list[dict] = []
    for item, entry in paired:
        assert by_key[item.sort_key] is item, "ordinal pairing lost a product"
        variant = entry["variant"]
        commission = variant["commission"]
        record = {
            "item_key": item.item_key,
            "ordinal": item.ordinal,
            "provider_key": spec.provider_key,
            "observed_at": item.observed_at.isoformat(),
            "parsed_date": item.observed_at.date().isoformat(),
            "name": variant["name"],
            "price": variant["price"],
            "commission": (
                None if commission is None
                else {**commission, "source_provider": spec.provider_key}
            ),
            "prices": variant["prices"],
            "spec_lines": [
                {**line, "source_provider": spec.provider_key}
                for line in _spec_lines(entry["text"], commission)
            ],
            "date_evidence": order.evidence_payload(),
            "text": entry["text"],
            # Placeholder so the record is complete before it is measured. The
            # signature hashes names and types, never values, so the placeholder does
            # not perturb the result -- but an absent field would report as `null` and
            # make every record's own signature disagree with its neighbours'.
            "shape_signature": "",
        }
        record["shape_signature"] = check_shape(record, signature)
        signature = record["shape_signature"]
        assert_provenance(record)
        final.append(record)

    # The output order is the ordinal order. That is the whole point of the exercise:
    # a reader can diff two runs' outputs and any difference is a real difference in
    # the inputs, not a reshuffle.
    final.sort(key=lambda record: record["ordinal"])

    report = SourceReport(
        provider_key=spec.provider_key,
        declared_bytes=chat.declared_bytes,
        entry_count=chat.entry_count,
        origin=chat.origin,
        field_order=order.order,
        proving_stamp=order.proving_stamps[0].raw if order.proving_stamps else "",
        ambiguous_stamps=order.ambiguous_stamps,
        messages=len(messages),
        products=len(final),
    )
    return final, report


def _describe(record: dict) -> str:
    return f"{record['item_key']} ({record['provider_key']}) {record['name'][:48]!r} @ {record['price']}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest one or more supplier chats into a deterministic product list."
    )
    parser.add_argument(
        "--sources",
        required=True,
        help="JSON file: {\"sources\":[{\"provider_key\",\"path\",\"label\"}],\"effective_tz\"}",
    )
    parser.add_argument("--work-dir", required=True, help="scratch directory for extracted chats")
    parser.add_argument("--out", required=True, help="where to write items.json")
    parser.add_argument(
        "--tz",
        default=None,
        help=f"overrides the run's effective_tz (default: the manifest's, else {limits.DEFAULT_EFFECTIVE_TZ})",
    )
    args = parser.parse_args(argv)

    manifest = json.loads(Path(args.sources).read_text(encoding="utf-8"))
    raw_sources = manifest.get("sources") or []
    if not raw_sources:
        print("manifest declares no sources", file=sys.stderr)
        return 2
    if len({entry.get("provider_key") for entry in raw_sources}) != len(raw_sources):
        print("two sources share a provider_key; R-012 requires distinct providers", file=sys.stderr)
        return 2

    specs = [
        SourceSpec(
            provider_key=entry["provider_key"],
            path=Path(entry["path"]),
            label=entry.get("label", ""),
        )
        for entry in raw_sources
    ]
    zone_name = args.tz or manifest.get("effective_tz") or limits.DEFAULT_EFFECTIVE_TZ

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    all_records: list[dict] = []
    reports: list[SourceReport] = []
    try:
        zone = dates.load_zone(zone_name)
        for slot, spec in enumerate(specs):
            records, report = ingest_source(spec, slot, work_dir, zone)
            all_records.extend(records)
            reports.append(report)
            print(
                f"{spec.provider_key:>18s}  {report.origin:>3s}  "
                f"{report.entry_count:>4d} entries  {report.declared_bytes:>7d} B  "
                f"order {report.field_order}  {report.products:>4d} products"
            )
    except (sources.SourceRejected, dates.DateRejected, IngestRejected) as exc:
        print(f"ingest failed: {exc}", file=sys.stderr)
        return 1

    # One shape across the whole run, not just within a source: R-022's scenario is a
    # one-source run and a four-source run agreeing, which only holds if the
    # cross-source check happens.
    signatures = sorted({record["shape_signature"] for record in all_records})
    if len(signatures) > 1:
        print(f"run produced {len(signatures)} distinct record shapes: {signatures}", file=sys.stderr)
        return 1

    payload = {
        "shape_signature": signatures[0] if signatures else "",
        "effective_tz": zone_name,
        "sources": [report.__dict__ for report in reports],
        "items": all_records,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys makes the bytes a function of the content alone, so the determinism
    # check is a plain diff of two files rather than a semantic comparison.
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )

    total = len(all_records)
    print(f"\n{total} products from {len(reports)} sources, one shape {signatures[0][:12]}")
    for report in reports:
        print(f"  {report.provider_key}: {report.products} products, proof {report.proving_stamp[:44]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())