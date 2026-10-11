"""The runner: one slice of one run, in three processes with three different environments.

A hosted runner is disposable -- when the job ends, the disk is gone. So a run is never
one long job. It is a chain of short ones (R-001), each of which wakes up, reads from the
database where the run is, does a bounded amount of work, writes it back, and asks for
the next one if anything is left. Everything a later slice needs is already in a row.

The three processes, and what each one is holding
-------------------------------------------------
``prepare``  **has the key, has no model.** Claims the run for this slice, checks that
             the prompt is still the one the run was created with, and downloads the
             owner's chats into the run directory. Writes no secret to disk.

``read``     **has the model, has no key.** Parses the chats, drops what is older than
             the run's effective date, and asks the model about the pairs code could not
             decide. This is the only process that touches supplier text and the only
             one a model runs in, and it refuses to start if a database variable is in
             scope. All it hands on is JSON.

``write``    **has the key, has no model.** Trusts nothing it was handed: it re-validates
             the product list, recomputes the consolidation itself from that list and
             the verdicts, and only then touches the database -- persisting the items,
             confronting them against the catalog, parking for approval or applying.

Which stages a slice runs is decided by ``runs.current_step``, never by the workflow:

    1-2  ingest + consolidate   prepare → read → write (persist; maybe ask for more)
    3    confront               write
    4    approval               nobody: the run waits for the owner (manual mode)
    5    apply                  write, in batches, until done or out of time
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import apply as _apply
from . import compare as _compare
from . import dbface as _dbface
from . import logmask as _logmask
from . import proposal as _proposal
from . import validate as _validate

KNOWLEDGE_BUCKET = "agent-private"
RULESET_OBJECT = "catalog/rules/catalog-rules.json"
PROMPT_OBJECT = "catalog/prompt/triage-prompt.md"
RUNS_BUCKET = "catalog-runs"

#: How long the model step may keep starting calls, and how long the apply loop may keep
#: claiming batches. The job's own ceiling is 12 minutes; these leave room for setup,
#: for the item in flight, and for asking for the next slice. A slice that works until
#: it is killed loses exactly the work it was doing when it died (R-031).
READ_BUDGET_SECONDS = 330.0
WRITE_BUDGET_SECONDS = 150.0

#: How many slices in a row may come back from the model with nothing new before the run
#: moves on with whatever is still unanswered left separate -- which is the conservative
#: default, and visible. A slice that made progress does not count: a long queue takes
#: several slices and that is not a failure. The hard ceiling on slices is the run's own.
MAX_TRIAGE_STALLS = 2

ITEM_KEY = re.compile(r"^[A-Za-z0-9_-]+-[0-9]{3,6}$")
SOURCE_SUFFIXES = (".zip", ".txt")

#: What a stage-3 item carries of the supplier's own announcement. ``date_evidence`` and
#: ``shape_signature`` are run-level facts and are not repeated on every row.
OFFER_KEYS = ("name", "price", "prices", "commission", "spec_lines", "text",
              "observed_at", "parsed_date", "ordinal")


class RunFailed(Exception):
    """This run cannot continue, and repeating the slice would not change that."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ────────────────────────────── plumbing ──────────────────────────────


def say(message: str) -> None:
    print(message, flush=True)


def set_output(name: str, value: str) -> None:
    """Hand a value to the workflow. A no-op outside a hosted runner."""
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    say(f"  [{name}={value}]")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True),
                         encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _client():
    from .postgrest import Client

    return Client.from_env()


class Store:
    """Private objects, from Storage in production and from a folder under test.

    The folder mode exists so the whole runner can be exercised against a local database
    with no Storage service. It is selected by a variable that is not a credential and is
    never set by the workflow.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        root = os.environ.get("CATALOG_LOCAL_STORAGE", "").strip()
        self._root = Path(root) if root else None

    def get(self, bucket: str, path: str) -> bytes:
        if self._root is not None:
            return (self._root / bucket / path).read_bytes()
        return self._client.storage_get(bucket, path)

    def exists(self, bucket: str, path: str) -> bool:
        from .postgrest import DatabaseRefused

        try:
            self.get(bucket, path)
            return True
        except FileNotFoundError:
            return False
        except DatabaseRefused as exc:
            if exc.status in (400, 404):
                return False
            raise

    def delete(self, bucket: str, paths: Sequence[str]) -> None:
        if self._root is not None:
            for path in paths:
                with contextlib.suppress(FileNotFoundError):
                    (self._root / bucket / path).unlink()
            return
        self._client.storage_delete(bucket, paths)

    def put(self, bucket: str, path: str, data: bytes) -> None:
        if self._root is not None:
            target = self._root / bucket / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return
        self._client.storage_put(bucket, path, data)


def fail_run(client: Any, run_id: str, code: str, message: str, event: str | None = None) -> None:
    """Close the run as FAILED with a reason the owner can read."""
    detail = {"code": code, "message": _logmask.scrub(message)[:600]}
    if event:
        with contextlib.suppress(Exception):
            client.run_event(run_id, event, detail)
    client.run_finish(run_id, "FAILED", {"error": detail})
    say(f"::error::run cerrado como FAILED · {code}")


def models_from_env() -> list[str]:
    raw = os.environ.get("CATALOG_MODELS", "")
    return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


#: The provider every model of the shared catalog is reached through.
MODEL_PROVIDER = "opencode"


def model_chain(client: Any) -> list[str]:
    """The owner's model first, then the workflow's fallbacks.

    Picking a model must never leave a run without one: if the chosen model is down or
    was withdrawn, the next pass of the model step moves on to the fallbacks. If the
    choice cannot even be read, the fallbacks are the chain and the run goes on.
    """
    fallbacks = models_from_env()
    try:
        chosen = client.chosen_model()
    except Exception:  # noqa: BLE001 - a missing setting is not a reason to stop a run
        return fallbacks
    qualified = chosen if "/" in chosen else f"{MODEL_PROVIDER}/{chosen}"
    return [qualified, *(model for model in fallbacks if model != qualified)]


# ────────────────────────────── retention ──────────────────────────────


def purge(client: Any, store: Store, limit: int = 20) -> int:
    """Delete the runs nobody has touched in days, files first and rows after.

    A run is a work record, not business data: what it applied is in the catalog. Kept
    forever it is one row per product ever read, supplier text included.

    Files go first because the rows are the only place their paths are written down. A
    cut between the two leaves rows without files, which the next pass finishes; the
    other order would leave files nobody can name. A run whose files could not be
    deleted keeps its rows and is tried again next time.
    """
    deleted = 0
    ready: list[str] = []
    for run in client.expired_runs(limit):
        run_id = str(run.get("id") or "")
        paths = [str(path) for path in (run.get("paths") or [])]
        try:
            if paths:
                store.delete(RUNS_BUCKET, paths)
        except Exception as exc:  # noqa: BLE001 - one stuck run must not keep the rest
            say(f"::warning::no se pudieron borrar los archivos de una corrida vencida: "
                f"{type(exc).__name__}")
            continue
        ready.append(run_id)
    if ready:
        deleted = client.purge_runs(ready)
    return deleted


def purge_quietly(client: Any, store: Store) -> None:
    """Retention as a side job of a slice: it must never be why a slice fails."""
    try:
        gone = purge(client, store)
        if gone:
            say(f"retención: {gone} corrida(s) antigua(s) borrada(s)")
    except Exception as exc:  # noqa: BLE001
        say(f"::warning::la limpieza de corridas antiguas no pudo correr: {type(exc).__name__}")


# ────────────────────────────── prepare ──────────────────────────────


def prepare(run_id: str, run_dir: Path) -> int:
    """Claim the run for this slice and stage what the model step needs."""
    client = _client()
    _logmask.register_mask(run_id)

    claim = client.claim_run(run_id)
    if not claim.claimed:
        info = client.read_run(run_id)["run"]
        if claim.budget_exhausted:
            # Left RUNNING on purpose: a run out of slices with work pending is a run
            # that needs looking at, and FAILED would hide that it is merely stuck.
            client.run_event(run_id, "SLICE_BUDGET_EXHAUSTED",
                             {"slice_count": claim.slice_count})
            say("::warning::el run agotó sus porciones con trabajo pendiente")
        else:
            say(f"el run está {info.get('status')}; esta porción no tiene nada que hacer")
        set_output("proceed", "false")
        set_output("need_read", "false")
        return 0

    run = claim.run or {}
    _logmask.register_all([run.get("business_id"), *(run.get("provider_keys") or [])])
    purge_quietly(client, Store(client))
    step = int(run.get("current_step") or 1)
    client.run_event(run_id, "SLICE_STARTED", {"slice": claim.slice_count, "step": step})

    run_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "run_id": run_id,
        "slice": claim.slice_count,
        "step": step,
        "mode": run.get("mode"),
        "effective_date": run.get("effective_date"),
        "effective_tz": run.get("effective_tz"),
        "models": model_chain(client),
    }
    need_read = step <= 2

    if need_read:
        store = Store(client)
        try:
            prompt = store.get(KNOWLEDGE_BUCKET, PROMPT_OBJECT)
        except Exception as exc:  # noqa: BLE001 - any failure here means no prompt
            fail_run(client, run_id, "PROMPT_UNAVAILABLE", str(exc))
            set_output("proceed", "false")
            set_output("need_read", "false")
            return 0
        if sha256(prompt) != run.get("prompt_sha256"):
            # The prompt changed after this run was created. Deciding half a catalog
            # under one set of instructions and half under another is not a run.
            fail_run(client, run_id, "PROMPT_MISMATCH",
                     "el prompt publicado ya no es el que este run fijó al crearse",
                     event="PROMPT_MISMATCH")
            set_output("proceed", "false")
            set_output("need_read", "false")
            return 0
        (run_dir / "prompt.md").write_bytes(prompt)

        sources = []
        for slot, source in enumerate(run.get("sources") or []):
            path = str(source.get("storage_path") or "")
            suffix = Path(path).suffix.lower()
            if suffix not in SOURCE_SUFFIXES:
                fail_run(client, run_id, "SOURCE_KIND_UNSUPPORTED",
                         f"la fuente {slot} no es .zip ni .txt")
                set_output("proceed", "false")
                set_output("need_read", "false")
                return 0
            # The local name is the slot number. The uploaded name is never used to
            # build a path on this machine.
            local = run_dir / "in" / f"{slot:02d}{suffix}"
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(store.get(RUNS_BUCKET, path))
            sources.append({"provider_key": source["provider_key"], "path": str(local),
                            "label": ""})
        # ``reference_at`` is when the run was created. A pasted message carries no
        # year; it is dated against this instant, which is frozen with the run, so every
        # slice of the run dates the same message the same way.
        write_json(run_dir / "manifest.json",
                   {"sources": sources, "effective_tz": run.get("effective_tz"),
                    "reference_at": run.get("created_at")})

        # What the model already answered in earlier slices, so it is not asked again.
        write_json(run_dir / "triage.json", {"verdicts": client.read_pairs(run_id)})

    write_json(run_dir / "state.json", state)
    say(f"porción {claim.slice_count} · paso {step} · {'con' if need_read else 'sin'} lectura")
    set_output("proceed", "true")
    set_output("need_read", "true" if need_read else "false")
    return 0


# ────────────────────────────── read ──────────────────────────────


def split_by_effective_date(items: Sequence[Mapping[str, Any]], effective_date: str
                            ) -> tuple[list[dict], list[dict]]:
    """R-021, strict: nothing announced before the effective date gets through.

    Both sides are aware instants -- the announcement carries the run's zone, the
    effective date is a ``timestamptz`` -- so the comparison needs no zone of its own.
    Strict is load-bearing: a stale announcement that slipped through would count as a
    confirmation, refresh its product, and keep it from ever going stale (R-028).
    """
    boundary = datetime.fromisoformat(effective_date.replace("Z", "+00:00"))
    kept: list[dict] = []
    dropped: list[dict] = []
    for item in items:
        observed = datetime.fromisoformat(str(item["observed_at"]))
        (dropped if observed < boundary else kept).append(dict(item))
    return kept, dropped


def read(run_dir: Path, cli: list[str] | None = None,
         budget: float = READ_BUDGET_SECONDS) -> int:
    """Parse, filter, and ask the model. No database, no key, nothing but JSON out."""
    from . import ingest as _ingest
    from . import triage as _triage

    _triage.assert_no_credentials()
    started = time.monotonic()
    state = read_json(run_dir / "state.json")
    result_path = run_dir / "read.json"

    # Supplier product names and supplier ids must not reach a public log, so the
    # stages' own chatter goes to a buffer and only counts are printed here.
    quiet = io.StringIO()
    with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        code = _ingest.main([
            "--sources", str(run_dir / "manifest.json"),
            "--work-dir", str(run_dir / "work" / "ingest"),
            "--out", str(run_dir / "items.json"),
        ])
    if code != 0:
        # Deterministic: the same files fail the same way next time. Say why and stop.
        reason = [line for line in quiet.getvalue().splitlines() if line.strip()][-1:] or [""]
        write_json(result_path, {"ok": False, "code": "INGEST_REJECTED",
                                 "message": _logmask.scrub(reason[0])[:600]})
        say("::error::la ingesta rechazó las fuentes")
        return 0

    items = read_json(run_dir / "items.json")["items"]
    kept, dropped = split_by_effective_date(items, state["effective_date"])
    say(f"ingesta: {len(items)} productos · {len(dropped)} anteriores a la fecha vigente")

    previous = read_json(run_dir / "triage.json").get("verdicts", [])
    verdicts = {entry["pair_id"]: entry for entry in previous}
    try:
        comparison = _compare.run(kept, verdicts)
    except _compare.CompareRejected as exc:
        write_json(result_path, {"ok": False, "code": "COMPARE_REJECTED",
                                 "message": _logmask.scrub(str(exc))[:600]})
        say("::error::la consolidación no cierra")
        return 0

    queue = [entry for entry in comparison.triage_queue if entry["pair_id"] not in verdicts]
    say(f"consolidación: {len(comparison.triage_queue)} pares para el modelo, "
        f"{len(queue)} sin responder")

    answered = 0
    batches: list[dict] = []
    if queue:
        document = comparison.as_dict()
        with contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            outcome = _triage.run(
                consolidation=document,
                prompt=run_dir / "prompt.md",
                work_dir=run_dir / "work" / "triage",
                out_path=run_dir / "triage.json",
                cli=cli,
                models=state.get("models") or [],
                resume=True,
                deadline=started + budget,
            )
        answered = len(outcome.verdicts) - len(previous)
        batches = [{"index": b.index, "status": b.status, "attempts": b.attempts,
                    "cli_exit": b.cli_exit, "error": _logmask.scrub(b.error)[:200]}
                   for b in outcome.batches]
        for batch in outcome.batches:
            if batch.status == "rejected":
                # The validator's sentence and the CLI's own error line: enough to tell a
                # dead endpoint from a malformed answer, and neither carries supplier text.
                say(f"  lote {batch.index} rechazado tras {batch.attempts} intentos: "
                    f"{_logmask.scrub(batch.error)[:160]}")
                for line in batch.cli_errors:
                    say(f"    {_logmask.scrub(line)}")
        say(f"modelo: {answered} veredictos nuevos · "
            f"{sum(1 for b in outcome.batches if b.status == 'rejected')} lotes rechazados · "
            f"{sum(1 for b in outcome.batches if b.status == 'deferred')} sin alcanzar")

    write_json(result_path, {"ok": True, "items": len(items), "answered_now": answered,
                             "batches": batches})
    return 0


# ────────────────────────────── write ──────────────────────────────


def validated_items(run_dir: Path, run: Mapping[str, Any]) -> list[dict]:
    """The product list the read step produced, checked before anything is built on it."""
    document = read_json(run_dir / "items.json")
    items = document.get("items")
    if not isinstance(items, list):
        raise RunFailed("ITEMS_MALFORMED", "items.json no trae una lista de productos")
    providers = set(run.get("provider_keys") or [])
    seen: set[str] = set()
    signatures: set[str] = set()
    for item in items:
        key = item.get("item_key") if isinstance(item, dict) else None
        if not isinstance(key, str) or not ITEM_KEY.match(key):
            raise RunFailed("ITEMS_MALFORMED", "un producto llegó sin item_key válido")
        if key in seen:
            raise RunFailed("ITEMS_MALFORMED", f"item_key repetido: {key}")
        seen.add(key)
        provider = item.get("provider_key")
        if provider not in providers or not key.startswith(f"{provider}-"):
            raise RunFailed("ITEMS_FOREIGN_PROVIDER",
                            "un producto no pertenece a un proveedor de esta corrida")
        try:
            datetime.fromisoformat(str(item.get("observed_at")))
        except ValueError:
            raise RunFailed("ITEMS_MALFORMED", f"fecha ilegible en {key}") from None
        if not isinstance(item.get("name"), str) or not isinstance(item.get("spec_lines"), list):
            raise RunFailed("ITEMS_MALFORMED", f"producto incompleto: {key}")
        signatures.add(str(item.get("shape_signature")))
    if len(signatures) > 1:
        raise RunFailed("ITEMS_SHAPE_DRIFT", "los productos no comparten una sola forma (R-022)")
    return items


def validated_verdicts(run_dir: Path, queue: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
    """The model's answers, kept only when they are about a pair this run really asked.

    This is the second gate over the same answers -- the read step already validated
    each batch -- and it is not redundant: that validation ran in the process the model
    ran in, and this one runs where the key is.
    """
    from . import triage as _triage

    path = run_dir / "triage.json"
    if not path.is_file():
        return {}
    wanted = {entry["pair_id"] for entry in queue}
    accepted: dict[str, dict] = {}
    for entry in read_json(path).get("verdicts", []):
        if not isinstance(entry, dict):
            continue
        pair = entry.get("pair_id")
        if pair not in wanted or entry.get("verdict") not in _triage.VERDICTS:
            continue
        evidence = entry.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            continue
        accepted[pair] = {"pair_id": pair, "verdict": entry["verdict"],
                          "evidence": evidence.strip()[:900],
                          "model": str(entry.get("model") or "")[:120] or None}
    return accepted


def _offer(item: Mapping[str, Any]) -> dict:
    return {key: item.get(key) for key in OFFER_KEYS}


def persist_items(client: Any, run_id: str, items: Sequence[Mapping[str, Any]],
                  dropped: Sequence[Mapping[str, Any]], comparison: Any) -> dict[str, int]:
    """One row per product, whatever became of it (R-030, R-031).

    Every input product gets a row: the ones that go on to the catalog, the ones another
    supplier's offer absorbed, the ones that were too old. Nothing leaves the population
    without saying why.
    """
    by_key = {item["item_key"]: item for item in items}
    rows: list[dict] = []
    forward: list[dict] = []

    for item in dropped:
        rows.append({
            "item_key": item["item_key"], "provider_key": item["provider_key"],
            "observed_at": item["observed_at"], "stage": 1, "status": "SKIPPED",
            "reason": "BEFORE_EFFECTIVE_DATE",
            "payload": {"name": item.get("name"), "price": item.get("price"),
                        "observed_at": item.get("observed_at")},
        })

    for key in sorted(comparison.dispositions):
        disposition = comparison.dispositions[key]
        item = by_key[key]
        base = {"item_key": key, "provider_key": item["provider_key"],
                "observed_at": item["observed_at"]}
        kind = disposition["disposition"]
        if kind == _compare.DUPLICATE:
            rows.append({**base, "stage": 1, "status": "SKIPPED",
                         "reason": "DUPLICATE_IN_SOURCE",
                         "payload": {"name": item.get("name"), "price": item.get("price")}})
        elif kind == _compare.CONSOLIDATED:
            rows.append({**base, "stage": 2, "status": "SKIPPED",
                         "reason": "CONSOLIDATED_INTO_OTHER",
                         "payload": {"name": item.get("name"), "price": item.get("price"),
                                     "winner_item_key": disposition.get("winner_item_key"),
                                     "group_id": disposition.get("group_id")}})
        else:
            merged = comparison.merged[key]
            row = {**base, "stage": 3, "status": "PENDING", "payload": {
                "offer": _offer(item),
                "details": merged["details"],
                "conditions": merged["conditions"],
                "group": {"absorbed": merged["absorbed"],
                          "transferred": merged["transferred"],
                          "withheld": len(merged["withheld"])},
            }}
            rows.append(row)
            forward.append(row)

    expected = len(items) + len(dropped)
    if len(rows) != expected or len({row["item_key"] for row in rows}) != expected:
        raise RunFailed("FUNNEL_UNRECONCILED",
                        f"{expected} productos entraron y {len(rows)} tienen destino")

    if forward:
        # R-027, checked by the database over the very payloads about to be stored: no
        # supplier-scoped value on a product from another supplier, nothing untagged.
        client.assert_provenance(run_id, [
            {"item_key": row["item_key"], "provider_key": row["provider_key"],
             "payload": row["payload"]} for row in forward])
    client.checkpoint_items(run_id, rows)

    return {
        "parsed": expected,
        "before_effective_date": len(dropped),
        "duplicates": sum(1 for row in rows if row.get("reason") == "DUPLICATE_IN_SOURCE"),
        "consolidated": sum(1 for row in rows if row.get("reason") == "CONSOLIDATED_INTO_OTHER"),
        "forward": len(forward),
    }


def consolidate(client: Any, run_id: str, run: Mapping[str, Any], run_dir: Path) -> bool:
    """Steps 1-2 on the database side. Returns whether the run may move on to step 3."""
    outcome = read_json(run_dir / "read.json") if (run_dir / "read.json").is_file() else None
    if outcome is None:
        raise RuntimeError("la lectura no dejó resultado; la porción se repite")
    if not outcome.get("ok"):
        raise RunFailed(str(outcome.get("code") or "READ_FAILED"),
                        str(outcome.get("message") or "la lectura falló"))

    items = validated_items(run_dir, run)
    kept, dropped = split_by_effective_date(items, str(run["effective_date"]))

    # Asked once without verdicts to learn the queue, then with the verdicts that are
    # really about that queue. The consolidation that gets stored is computed here.
    queue = _compare.run(kept, {}).triage_queue
    verdicts = validated_verdicts(run_dir, queue)
    comparison = _compare.run(kept, verdicts)

    if verdicts:
        client.save_pairs(run_id, [verdicts[pair] for pair in sorted(verdicts)])

    unresolved = [entry["pair_id"] for entry in queue if entry["pair_id"] not in verdicts]
    before = (run.get("result") or {}).get("triage") or {}
    progressed = len(verdicts) > int(before.get("answered") or 0)
    stalls = 0 if progressed else int(before.get("stalls") or 0) + 1
    triage = {"pairs": len(queue), "answered": len(verdicts), "unresolved": len(unresolved),
              "rounds": int(before.get("rounds") or 0) + 1, "stalls": stalls}
    client.run_progress(run_id, {"triage": triage})

    if unresolved and stalls < MAX_TRIAGE_STALLS:
        say(f"modelo: quedan {len(unresolved)} pares de {len(queue)}; se pide otra porción")
        return False
    if unresolved:
        # The model stopped answering. The unanswered pairs stay separate, which is the
        # safe side of the default, and the number is in the run for anyone to see.
        client.run_event(run_id, "LLM_EXIT_ANOMALY", {"unresolved_pairs": len(unresolved)})

    counts = persist_items(client, run_id, kept, dropped, comparison)
    funnel = dict(comparison.funnel)
    client.run_progress(run_id, {
        "ingest": {"parsed": counts["parsed"],
                   "before_effective_date": counts["before_effective_date"]},
        "consolidate": {
            "examined": funnel.get("parsed"),
            "duplicates": counts["duplicates"],
            "consolidated": counts["consolidated"],
            "products_out": counts["forward"],
            "pairs": funnel.get("pairs"),
            "triage_same": funnel.get("triage_same"),
            "triage_different": funnel.get("triage_different"),
            "triage_unresolved": funnel.get("triage_unresolved"),
        },
    })
    client.run_step_done(run_id, 3)
    client.run_event(run_id, "STEP_DONE", {"step": 2, **counts})
    say(f"persistidos {counts['parsed']} productos · {counts['forward']} siguen al catálogo")
    return True


def catalog_snapshot(client: Any, store: Store, run: Mapping[str, Any]) -> dict:
    """The tenant's catalog, read once per run and reused by every later slice (R-035).

    Scoped to the run's own business: products reach a business through their catalog,
    and reading the whole table would confront this run against other tenants' products.
    """
    from .postgrest import IN_FILTER_CHUNK

    path = f"{run['business_id']}/{run['id']}/work/snapshot.json"
    if store.exists(RUNS_BUCKET, path):
        return json.loads(store.get(RUNS_BUCKET, path))

    catalogs = client.select_all("catalogs", eq={"business_id": run["business_id"]},
                                 columns=("id",), order=("id", "asc"))
    catalog_ids = [row["id"] for row in catalogs]
    products: list[dict] = []
    for start in range(0, len(catalog_ids), IN_FILTER_CHUNK):
        products.extend(client.select_all(
            "products", contains={"catalog_id": catalog_ids[start:start + IN_FILTER_CHUNK]},
            columns=("id", "catalog_id", "name", "base_price", "currency",
                     "provider_public_key", "created_at", "updated_at"),
            order=("id", "asc")))
    product_ids = [row["id"] for row in products]

    def children(table: str, columns: tuple[str, ...]) -> list[dict]:
        rows: list[dict] = []
        for start in range(0, len(product_ids), IN_FILTER_CHUNK):
            rows.extend(client.select_all(
                table, contains={"product_id": product_ids[start:start + IN_FILTER_CHUNK]},
                columns=columns, order=("id", "asc")))
        return rows

    snapshot = {
        "taken_at": datetime.now().astimezone().isoformat(),
        "products": products,
        "details": children("product_details", ("id", "product_id", "title", "details", "value")),
        "conditions": children("product_conditions",
                               ("id", "product_id", "icon", "title", "details", "sort_order")),
        "commissions": children("product_commissions",
                                ("id", "product_id", "commission_type", "value", "notes")),
    }
    store.put(RUNS_BUCKET, path,
              json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return snapshot


def confront(client: Any, store: Store, run_id: str, run: Mapping[str, Any],
             ruleset: Mapping[str, Any]) -> int:
    """Step 3: one verdict and one proposal per product, stored with the product.

    Returns how many products were confronted. Zero is a legitimate answer -- every
    announcement was older than the effective date, say -- and it means there is nothing
    to approve and nothing to apply.
    """
    snapshot = catalog_snapshot(client, store, run)
    catalog = _dbface.load_catalog(snapshot["products"], snapshot["details"],
                                   snapshot["commissions"], snapshot["conditions"])
    by_product = {entry.product_id: entry for entry in catalog}
    taken_at = datetime.fromisoformat(snapshot["taken_at"]).timestamp()

    # Stage 4 items are read too: a slice that died half-way through writing verdicts
    # must reach the same assignment the second time, and the assignment is global.
    stored = [item for item in client.read_items(run_id, stages=[3, 4])
              if item.get("status") == "PENDING"]
    if not stored:
        return 0
    records = []
    for item in stored:
        offer = (item.get("payload") or {}).get("offer") or {}
        records.append({**offer, "item_key": item["item_key"],
                        "provider_key": item["provider_key"]})
    result = _dbface.confront(records, catalog, now=taken_at)

    icons = _proposal.learn_icons(catalog)
    payloads = {item["item_key"]: item.get("payload") or {} for item in stored}
    providers = {item["item_key"]: item["provider_key"] for item in stored}
    rows: list[dict] = []
    proposals: list[dict] = []
    for verdict in result.verdicts:
        payload = payloads[verdict.item_key]
        proposed = _proposal.build(
            run_id, verdict, payload.get("offer") or {}, payload.get("conditions") or [],
            by_product.get(verdict.product_id or ""), icons)
        if proposed is not None:
            proposals.append(proposed)
        rows.append({
            "item_key": verdict.item_key, "stage": 4, "status": "PENDING",
            "action": verdict.action,
            "patch": {"verdict": verdict.as_dict(), "proposal": proposed},
        })

    # The gate reads the proposals now, before anyone is asked to approve them. An item
    # the gate would refuse at apply time must not be shown as approvable.
    gate = _validate.gate([_proposal.write_row(p) for p in proposals], ruleset=ruleset,
                          run_providers=run.get("provider_keys") or [],
                          item_providers=providers)
    refused = gate.refused_items()
    for row in rows:
        if row["item_key"] in refused:
            row["status"] = "FAILED"
            row["patch"]["failure"] = [f.as_dict() for f in gate.for_item(row["item_key"])]

    client.advance_items(run_id, rows)
    funnel = dict(result.funnel)
    funnel["refused_by_gate"] = len(refused)
    client.run_progress(run_id, {"confront": funnel, "snapshot_taken_at": snapshot["taken_at"]})
    client.run_event(run_id, "VALIDATED", {"step": 3, **{
        key: funnel[key] for key in ("incoming", "to_update", "to_touch", "to_return",
                                     "no_change", "needs_review", "refused_by_gate")}})
    say(f"confrontación: {funnel['to_update']} a actualizar · {funnel['to_touch']} a refrescar · "
        f"{funnel['to_return']} nuevos · {funnel['no_change']} sin cambio")
    return len(stored)


def apply_stage(client: Any, run_id: str, run: Mapping[str, Any],
                ruleset: Mapping[str, Any], deadline: float) -> bool:
    """Step 5. Returns whether work is left for another slice."""
    written = resumed = failed = 0
    while time.monotonic() < deadline:
        claimed = client.claim_items(run_id, 5)
        if not claimed:
            break
        try:
            outcome = _apply.apply_batch(client, run_id, claimed, ruleset=ruleset,
                                         run_providers=run.get("provider_keys") or [])
        except _apply.RunStopped:
            say("el run dejó de estar en curso; la porción no escribe más")
            return False
        written += len(outcome.written)
        resumed += len(outcome.resumed)
        failed += len(outcome.failed)
        client.run_event(run_id, "APPLY_WRITTEN", {
            "written": len(outcome.written), "resumed": len(outcome.resumed),
            "failed": len(outcome.failed)})
    say(f"aplicación: {written} escritos · {resumed} reanudados · {failed} fallidos")

    counts = client.read_run(run_id)["counts"]
    return any(row["stage"] == 5 and row["status"] in ("PENDING", "IN_FLIGHT")
               for row in counts)


def summary(client: Any, run_id: str) -> dict:
    """What the run did, counted off the rows themselves rather than remembered."""
    items = client.read_items(run_id)
    final = [item for item in items if item.get("stage") == 5]

    def count(predicate) -> int:
        return sum(1 for item in final if predicate(item))

    return {"summary": {
        "total": len(items),
        "updated": count(lambda i: i["status"] == "DONE" and i.get("action") == "update"),
        "refreshed": count(lambda i: i["status"] == "DONE" and i.get("action") == "touch"),
        "returned_to_client": count(lambda i: i["status"] == "DONE" and i.get("action") == "new"),
        "rejected": count(lambda i: i.get("reason") == "REJECTED_BY_OWNER"),
        "needs_review": count(lambda i: i.get("reason") == "NEEDS_OWNER_REVIEW"),
        "no_change": count(lambda i: i.get("reason") in ("CATALOG_OFFER_KEPT", "IMPLAUSIBLE_PRICE")),
        "failed": sum(1 for item in items if item.get("status") == "FAILED"),
        "skipped_before_catalog": sum(1 for item in items if item.get("stage") in (1, 2)),
    }}


def load_ruleset(store: Store, run: Mapping[str, Any]) -> dict:
    """The ruleset, re-fetched every slice and compared against the run's pin."""
    try:
        data = store.get(KNOWLEDGE_BUCKET, RULESET_OBJECT)
    except Exception as exc:  # noqa: BLE001
        raise RunFailed("RULESET_UNAVAILABLE", str(exc)) from None
    if sha256(data) != run.get("ruleset_sha256"):
        raise RunFailed("RULESET_MISMATCH",
                        "las reglas publicadas ya no son las que este run fijó al crearse")
    return json.loads(data)


def write(run_id: str, run_dir: Path, budget: float = WRITE_BUDGET_SECONDS) -> int:
    """Everything that needs the key: persist, confront, park or apply."""
    deadline = time.monotonic() + budget
    client = _client()
    _logmask.register_mask(run_id)
    info = client.read_run(run_id)
    run = info["run"]
    _logmask.register_all([run.get("business_id"), *(run.get("provider_keys") or [])])

    redispatch = False
    if run.get("status") != "RUNNING":
        say(f"el run está {run.get('status')}; nada que escribir")
        set_output("redispatch", "false")
        return 0

    store = Store(client)
    try:
        ruleset = load_ruleset(store, run)
        step = int(run.get("current_step") or 1)

        if step <= 2:
            if not consolidate(client, run_id, run, run_dir):
                set_output("redispatch", "true")
                client.run_event(run_id, "REDISPATCHED", {"reason": "triage"})
                return 0
            step = 3
            run = client.read_run(run_id)["run"]

        if step == 3:
            confronted = confront(client, store, run_id, run, ruleset)
            if confronted == 0:
                # Nothing reached the catalog. Parking a manual run to wait for the
                # approval of nothing would leave it waiting forever.
                client.run_step_done(run_id, 5)
                client.run_finish(run_id, "COMPLETED", summary(client, run_id))
                say("ningún producto llegó al catálogo; run COMPLETED")
                set_output("redispatch", "false")
                return 0
            if run.get("mode") == "manual":
                client.run_finish(run_id, "AWAITING_APPROVAL",
                                  {"awaiting_since": datetime.now().astimezone().isoformat()})
                say("el run espera la aprobación del owner")
                set_output("redispatch", "false")
                return 0
            approved = client.auto_approve(run_id)
            say(f"aprobado por el agente: {approved.get('to_apply')} a aplicar")
            step = 5

        if step == 4:
            # A RUNNING run at step 4 is a manual run whose approval was interrupted
            # between two statements. It is the owner's to approve again.
            client.run_finish(run_id, "AWAITING_APPROVAL", None)
            set_output("redispatch", "false")
            return 0

        if step == 5:
            redispatch = apply_stage(client, run_id, run, ruleset, deadline)
            if not redispatch and client.read_run(run_id)["run"].get("status") == "RUNNING":
                client.run_finish(run_id, "COMPLETED", summary(client, run_id))
                say("run COMPLETED")

    except RunFailed as exc:
        event = exc.code if exc.code in ("RULESET_MISMATCH", "PROVENANCE_VIOLATION") else None
        fail_run(client, run_id, exc.code, str(exc), event=event)
        set_output("redispatch", "false")
        return 0
    except _compare.CompareRejected as exc:
        fail_run(client, run_id, "COMPARE_REJECTED", str(exc))
        set_output("redispatch", "false")
        return 0
    except _dbface.ConfrontationRejected as exc:
        fail_run(client, run_id, "CONFRONT_REJECTED", str(exc))
        set_output("redispatch", "false")
        return 0
    except _validate.GateRefused as exc:
        fail_run(client, run_id, "RULESET_UNUSABLE", str(exc))
        set_output("redispatch", "false")
        return 0
    except Exception as exc:  # noqa: BLE001
        # A supplier's term on another supplier's product (R-027) is refused by the
        # database itself. That is a verdict on the data, not a transient error.
        if getattr(exc, "code", "") == "U0197":
            fail_run(client, run_id, "PROVENANCE_VIOLATION", str(exc),
                     event="PROVENANCE_VIOLATION")
            set_output("redispatch", "false")
            return 0
        raise

    if redispatch:
        client.run_event(run_id, "REDISPATCHED", {"reason": "apply"})
    set_output("redispatch", "true" if redispatch else "false")
    return 0


# ────────────────────────────── entry ──────────────────────────────


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catalog.run")
    parser.add_argument("phase", choices=("prepare", "read", "write", "purge"))
    parser.add_argument("--run-id", default="")
    parser.add_argument("--dir", default="", help="directorio de trabajo de esta porción")
    parser.add_argument("--cli", default=None,
                        help="(read) comando del modelo; se parte por espacios, sin shell")
    parser.add_argument("--budget", type=float, default=None)
    args = parser.parse_args(argv)
    run_dir = Path(args.dir)

    try:
        if args.phase == "purge":
            client = _client()
            total = 0
            # A backlog is worked off in a few passes; a pass that deletes nothing ends it.
            for _ in range(10):
                gone = purge(client, Store(client))
                total += gone
                if gone == 0:
                    break
            say(f"retención: {total} corrida(s) borrada(s)")
            return 0
        if not args.dir:
            say("::error::falta --dir")
            return 2
        if args.phase == "read":
            return read(run_dir, cli=args.cli.split() if args.cli else None,
                        budget=args.budget or READ_BUDGET_SECONDS)
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", args.run_id or ""):
            say("::error::run-id no es un uuid")
            return 2
        if args.phase == "prepare":
            return prepare(args.run_id, run_dir)
        return write(args.run_id, run_dir, budget=args.budget or WRITE_BUDGET_SECONDS)
    except Exception as exc:  # noqa: BLE001 - the last line of defence for the log
        # The message may carry an item key or a supplier id. It is scrubbed, and the
        # traceback -- which would print values -- is deliberately not shown.
        say(f"::error::{type(exc).__name__}: {_logmask.scrub(str(exc))[:400]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
