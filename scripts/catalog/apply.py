"""Step 5: write what was approved, one bounded batch at a time (R-030, R-031).

Three things happen to a batch, in this order, and the order is the safety property:

1. **The gate runs first.** ``validate.gate`` reads the exact rows about to be sent. A
   refused row is not written and its item is marked ``FAILED`` with the reasons; the
   rest of the batch goes on (R-008).
2. **Writes happen in three legs** -- the catalog, then the stamp, then ``DONE`` -- as
   three separate statements. A crash between the first and the second replays the
   write, which is harmless because every write here is idempotent: a price is set, not
   incremented, and inserted terms carry deterministic ids. A crash between the second
   and the third leaves a stamped item, and the stamp is how the next slice knows not to
   write it again.
3. **A row the database refuses fails alone.** The batch is one transaction, so one bad
   row aborts all twenty; they are then retried one by one, and only the row that is
   actually wrong ends up ``FAILED``.

Products with no counterpart never reach this module: they were closed at approval and
go back to the owner (R-011).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import validate as _gate
from .postgrest import Client, DatabaseRefused
from .proposal import write_row

#: The run stopped being RUNNING under us (the owner cancelled). Not an item failure:
#: the slice stops, and nothing else is written.
RUN_NOT_RUNNING = "U0207"


class RunStopped(Exception):
    """The run is no longer RUNNING; the slice must stop writing."""


@dataclass
class BatchOutcome:
    """What one claimed batch turned into."""

    written: list[str] = field(default_factory=list)      # item_keys applied now
    resumed: list[str] = field(default_factory=list)      # stamped earlier, closed now
    failed: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def settled(self) -> int:
        return len(self.written) + len(self.resumed) + len(self.failed)


def _fail(client: Client, run_id: str, failures: Mapping[str, list[dict]]) -> None:
    if not failures:
        return
    client.advance_items(run_id, [
        {"item_key": key, "status": "FAILED", "patch": {"failure": reasons}}
        for key, reasons in sorted(failures.items())
    ])


def apply_batch(
    client: Client,
    run_id: str,
    claimed: Sequence[Mapping[str, Any]],
    *,
    ruleset: Mapping[str, Any],
    run_providers: Sequence[str],
) -> BatchOutcome:
    """Carry one claimed batch through gate, write, stamp and close."""
    outcome = BatchOutcome()

    # An item stamped by a slice that died before closing it: the write already landed.
    stamped = [item for item in claimed if item.get("applied_at")]
    pending = [item for item in claimed if not item.get("applied_at")]

    rows: list[dict] = []
    providers: dict[str, str] = {}
    for item in pending:
        key = str(item["item_key"])
        proposal = (item.get("payload") or {}).get("proposal")
        if not isinstance(proposal, Mapping):
            outcome.failed[key] = [{"code": "proposal-missing", "where": key,
                                    "detail": "el ítem llegó a la aplicación sin propuesta"}]
            continue
        rows.append(write_row(proposal))
        providers[key] = str(item.get("provider_key") or "")

    verdict = _gate.gate(rows, ruleset=ruleset, run_providers=run_providers,
                         item_providers=providers)
    for key in sorted(verdict.refused_items()):
        outcome.failed[key] = [f.as_dict() for f in verdict.for_item(key)]
    rows = [row for row in rows if row["item_key"] not in verdict.refused_items()]

    applied: list[dict] = []
    if rows:
        try:
            client.apply_products(run_id, rows)
            applied = rows
        except DatabaseRefused as exc:
            if exc.code == RUN_NOT_RUNNING:
                raise RunStopped(str(exc)) from None
            # One row is wrong and the transaction took the rest with it. Find it.
            for row in rows:
                try:
                    client.apply_products(run_id, [row])
                    applied.append(row)
                except DatabaseRefused as single:
                    if single.code == RUN_NOT_RUNNING:
                        raise RunStopped(str(single)) from None
                    outcome.failed[row["item_key"]] = [{
                        "code": single.code or "database-refused",
                        "where": row["item_key"],
                        "detail": single.detail or str(single),
                    }]

    _fail(client, run_id, outcome.failed)

    if applied:
        # Leg two, for what actually went in and nothing else: a stamp on an unwritten
        # row is a lie that would suppress the replay it exists to make safe.
        client.stamp_applies(run_id, [
            {"item_key": row["item_key"], "product_id": row["product_id"]} for row in applied
        ])
    closing = [{"item_key": row["item_key"]} for row in applied] + [
        {"item_key": str(item["item_key"])} for item in stamped
    ]
    if closing:
        client.mark_done(run_id, closing)

    outcome.written = [row["item_key"] for row in applied]
    outcome.resumed = [str(item["item_key"]) for item in stamped]
    return outcome


__all__ = ["RUN_NOT_RUNNING", "RunStopped", "BatchOutcome", "apply_batch"]
