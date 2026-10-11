"""The one door between this runner and the database (R-003, R-007, R-031, §7.1, §7.3).

Every row this pipeline writes goes through here, and there is no second door.
That is the design, not an accident of layering: the requirements forbid building
SQL out of supplier text (§7.3), and the cheapest way to honour that is for the
process to have no way to express SQL at all. There is no ``service_execute_sql``
method here and no ``query()`` that takes a string. A table name comes from a
closed set of constants in this file, a column name comes from a format check,
and a value travels as JSON -- so a product description containing ``;`` arrives
at the other end as it left. That character is the whole reason this path exists:
``service_execute_sql`` rejects ``;`` outright, which is what pushed
the legacy script into replacing every semicolon in every string column with
a comma. (That historical corruption is filed as
a separately filed bug and is not fixed by this file.)

**Which half of the run this belongs to.** The extraction step reads a supplier
chat and runs with no ``SUPABASE_*`` variable in its environment at all -- not
unused, absent (§7.1). This module is the deterministic half. Importing it there
is safe and inert: it reads no environment variable, opens no socket and builds
no client at import time. ``Client.from_env()`` is where the partition is
enforced, and it raises rather than degrading to an anonymous client, because a
client that quietly falls back to a lesser role is a client whose authorisation
nobody is reasoning about. The extraction step's own assertion stays the shell
one it should be -- ``env | grep -c '^SUPABASE_'`` must be zero -- and this
module's contribution is that there is nothing here to invoke if it is not.

**Nothing in here prints.** Every diagnostic that reaches the Actions log goes
through the caller, which is where ``::add-mask::`` lives (§7.4). That matters
more than it sounds: the error body of ``agent_catalog_checkpoint_items`` names
the offending ``item_key`` values in its ``detail``, and an ``item_key`` embeds a
real provider id. A bare ``except: print(exc)`` added to this file later would
put that on a log anybody can read without logging in.

**What is deliberately not here.** Storage, because the ruleset fetch and the
chat download belong to the modules that consume them (``rules.py``,
``sources.py``) and a generic storage method would only be a second way to
decide a path. ``owner_get_catalog_run``, because it authenticates an owner
through ``mvp_authenticate_owner`` and this process has no user JWT -- it holds
``service_role``, which would be the wrong credential for a call whose entire
purpose is proving the caller *is* the owner. That RPC is the client read path
and belongs to Phase 8.

**R-003.** This file is written to sit in the public repo, which is where the
design puts it and where the free runner minutes are. It holds no value
catalogue, no provider key, no product data and no customer text; the knowledge
channel is private Storage, which this client does not touch. What it does name
is four tables of the app's own schema, and that is a disclosure -- so four is
the whole list, and widening it should be a decision somebody makes on purpose.
Two of the four were added in phase 6 with the reasons written next to
``TABLE_NAMES``; neither can be written, and that is enforced by
``CONFLICT_TARGETS`` rather than promised here.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping, Sequence

# ── the credential partition ────────────────────────────────────────────────

ENV_URL = "SUPABASE_URL"
# The design calls the secret SUPABASE_SERVICE_ROLE; every workflow that already
# exists in this project names the same secret SUPABASE_SERVICE_KEY. Both are
# accepted and neither is a fallback for anything else -- the point of the §7.1
# assertion is that zero SUPABASE_* variables exist in the extraction step, so the
# count is unaffected by which of the two the deterministic step uses.
ENV_SERVICE_ROLE = "SUPABASE_SERVICE_ROLE"
ENV_SERVICE_KEY = "SUPABASE_SERVICE_KEY"
# Not a credential and not needed in production: a bare PostgREST (the local test
# stack) serves at the root, while the hosted gateway serves it under /rest/v1.
ENV_REST_PREFIX = "CATALOG_REST_PREFIX"

# No project URL is baked in. `the legacy script` hardcodes one, which means the
# file cannot be reused against another environment and leaks the project ref
# into any repo it is copied to. Same for the `.env.local` fallback at
# `the legacy script`: reading a developer's key file is exactly the shape of
# the committed-template leak R-020 forbids, just one directory over.

# ── batch and transport constants ───────────────────────────────────────────

# Mirrors `agent_catalog.assert_batch`, which raises BATCH_TOO_LARGE above 20.
# The database is the invariant; this copy exists so the mistake costs a local
# message instead of a round trip, and so the number that sizes the call is a
# named constant rather than a literal typed at each call site.
MAX_BATCH_ROWS = 20

# Measured Supabase round trip is 0.58-1.00 s. Thirty seconds is roughly thirty
# times the worst observed call and about half the legacy 60 s, because nothing
# here calls `service_execute_sql` and therefore is not under its 15 s server
# timeout. A slice has a 540 s budget that it checks at product boundaries, so a
# hung socket should fail fast and leave the budget to the caller.
REQUEST_TIMEOUT_SECONDS = 30

# `service_execute_sql` has its own 15 s cap, and a refused multi-statement body
# is truncated in the Actions log before anybody can read it. The diagnostics that
# matter to a human are in `detail`, so keep a generous slice of the body and let
# the caller decide what to re-emit.
MAX_ERROR_BODY_CHARS = 2000

# ── code-owned identifiers ──────────────────────────────────────────────────

# Closed on purpose. PostgREST exposes `public` only (confirmed live:
# `pgrst.db_schemas` is unset and GET /rest/v1/runs answers PGRST205), so the
# runner's own tables are reachable only through the `agent_catalog` wrappers and
# appear nowhere in this set. A table name decides *which data* a call can touch,
# so it gets a membership test. A column name only narrows or widens what one of
# those tables already gave us, and duplicating the real column list here would be
# the R-020 embedding in a different costume -- app structure, frozen in code,
# guaranteed to drift. Columns get a format check instead, which is enough to
# stop injection and not enough to need maintaining.
#
# `catalogs` and `product_commissions` were added in phase 6 and both are
# **read-only**, which is structural rather than a promise: neither appears in
# ``CONFLICT_TARGETS``, so ``upsert()`` and ``compare_and_set()`` refuse them by
# name (``.get(table, ())`` is empty) and there is no call in this client that can
# write to either one. That matters, because `product_commissions` is otherwise
# absent on purpose -- its upsert is per product (UNIQUE(product_id)) and happens
# inside ``agent_catalog_apply_products`` -- and adding a table here must not have
# quietly added a second way to write a commission.
#
# They are here for two measured reasons, both from the catalog snapshot of R-035:
#
# * **`catalogs` is the tenant boundary.** ``products`` carries ``catalog_id``, not
#   ``business_id``; the owner lives one hop away. The legacy snapshot
#   (the legacy script) read *every* product in the project, which today
#   means 206 rows of which 12 belong to a different business. A confrontation that
#   compared this run's products against another tenant's catalog would produce a
#   funnel that looks complete and is not.
# * **`product_commissions` is R-026's tie-break.** Commission only decides on an
#   exact price tie, and without the database's own commission an exact tie has no
#   winner and silently degrades into "keep what is there".
#
# The alternative for both is a `public.agent_catalog_*` wrapper that returns the
# scoped rows as one jsonb document. That is fewer round trips and it puts the
# tenant check in SQL rather than in this file, which is the better shape; it is
# phase 2 work and it is not done here. What is done here is the narrower door,
# opened on purpose, with no write path.
TABLE_NAMES = frozenset(
    {"catalogs", "product_commissions", "product_conditions", "product_details", "products"}
)

# `run_items` is absent because PostgREST cannot see the schema at all.
CONFLICT_TARGETS = {"products": ("id",), "product_details": ("id",)}

# The paging size for a full-table read, and the largest ``in.()`` list this client
# will put in one URL. Neither is the write batch cap: ``MAX_BATCH_ROWS`` is a
# database invariant enforced by ``agent_catalog.assert_batch``, and reusing that
# number here would make a read look like it is governed by the same rule.
#
# The page size is measured, not guessed. PostgREST answers
# ``GET /rest/v1/product_details?limit=100000`` with exactly 1000 rows and a
# ``Content-Range`` of ``0-999/*`` -- confirmed live against production on
# 2026-10-09, where the table holds 1337 rows. A read that trusted its own limit
# would stop at 1000 and report a catalog missing 25% of its ficha, with nothing
# in the answer to say so.
PAGE_ROWS = 1000
IN_FILTER_CHUNK = 50

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9._-]{2,80}$")

# ── the runner's own event vocabulary ───────────────────────────────────────

# A mirror of the live `run_events_event_check`, which was read off production
# rather than off the design. It lives here so a typo costs no round trip and
# names itself; the CHECK remains the backstop. The two can drift, and the
# direction of the drift is the safe one: a stale code set refuses locally a
# value the database would have accepted, which is a loud failure at 0.6 s, never
# a silent corruption. To re-check after a schema change:
#
#   select pg_get_constraintdef(oid) from pg_constraint
#    where conname = 'run_events_event_check';
RUN_EVENTS = frozenset(
    {
        "RUN_CREATED",
        "STEP_STARTED",
        "STEP_DONE",
        "VALIDATED",
        "LLM_EXIT_ANOMALY",
        "APPLY_WRITTEN",
        "APPLY_STAMPED",
        "ITEM_DONE",
        "ITEM_FAILED",
        "AWAITING_APPROVAL",
        "OWNER_DECISION",
        "APPROVED",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "SLICE_BUDGET_EXHAUSTED",
        "RULESET_MISMATCH",
        "PROVENANCE_VIOLATION",
        "PROMPT_MISMATCH",
        "SLICE_STARTED",
        "REDISPATCHED",
    }
)

# The nine wrappers PostgREST can reach, confirmed live with their identities:
# every one is `security definer`, `revoke`d from anon and authenticated, and
# callable only by service_role. A name outside this set cannot be sent, so a
# model-authored value cannot pick which function runs.
RPC_FUNCTIONS = frozenset(
    {
        "agent_catalog_claim_run",
        "agent_catalog_claim_items",
        "agent_catalog_checkpoint_items",
        "agent_catalog_run_event",
        "agent_catalog_run_step_done",
        "agent_catalog_assert_provenance",
        "agent_catalog_apply_products",
        "agent_catalog_stamp_applies",
        "agent_catalog_mark_done",
        "agent_catalog_read_run",
        "agent_catalog_read_items",
        "agent_catalog_save_pairs",
        "agent_catalog_read_pairs",
        "agent_catalog_advance_items",
        "agent_catalog_run_progress",
        "agent_catalog_run_finish",
        "agent_catalog_auto_approve",
        "agent_catalog_model",
    }
)

# The two private buckets this runner may read, and the only one it may write.
# ``agent-private`` holds rules and prose and belongs to no business; ``catalog-runs``
# holds what the owner uploaded plus the run's own working files, under
# ``<business>/<run>/``. A bucket name outside this set cannot be requested.
STORAGE_READ_BUCKETS = frozenset({"agent-private", "catalog-runs"})
STORAGE_WRITE_BUCKETS = frozenset({"catalog-runs"})

# An object path is built from the run row and from constants in this package, never
# from supplier text. The check is here anyway: no traversal, no empty segment, and a
# conservative alphabet.
_OBJECT_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)+$")

# Largest object this client will pull into memory. The upload bucket caps a file at
# 64 MB; anything above that is not something the owner could have put there.
MAX_OBJECT_BYTES = 64 * 1024 * 1024


# ── failures ────────────────────────────────────────────────────────────────


class CredentialsAbsent(RuntimeError):
    """The deterministic half of the run has no credentials.

    Raised instead of degrading to an anonymous client. In the extraction step
    this exception *is* the control working: there is no ``SUPABASE_*`` variable
    there, so there is no client, so no chat content can reach the database
    however the model behaves (§7.1).
    """


class DatabaseRefused(RuntimeError):
    """The database answered with an error, or never answered.

    ``code`` is PostgREST's code or the SQLSTATE the wrapper raised
    (``ITEM_IDENTITY_FROZEN``, ``CATALOG_INSERT_FORBIDDEN``,
    ``PROVENANCE_VIOLATION``, ``CATALOG_FORBIDDEN``, ...). Callers branch on it
    rather than on prose, so a wording change upstream cannot change behaviour.
    ``detail`` carries whatever the server sent and may contain ``item_key``
    values, which embed provider ids -- treat it as log-bound, never as
    user-facing text.
    """

    def __init__(self, message: str, *, status: int = 0, code: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


class IdentifierRejected(ValueError):
    """A table, column or conflict target came from outside this module.

    §7.3 in one guard: model output is data, and data does not name tables.
    """


class BatchRejected(ValueError):
    """A batch was empty or over ``MAX_BATCH_ROWS``. The database would say the
    same thing; this says it without the round trip."""


class InsertForbidden(ValueError):
    """A batch carried ``action='new'``, which R-011 never writes.

    Named rather than raised as a bare ``ValueError`` because it is an expected
    per-item outcome rather than a bug: one product with no database counterpart
    is the normal shape of this pipeline, the runner marks that item ``FAILED``
    with its reason and keeps going (R-008), and to do that it has to be able to
    tell this apart from a mistake. The wrapper raises the database's own
    ``CATALOG_INSERT_FORBIDDEN``; catching it here first means the refusal lands
    before any of the batch is written rather than after.
    """


class ClaimLost(RuntimeError):
    """A compare-and-set matched a different number of rows than expected.

    This is the failure that a 0-row PATCH hides by default. Verified live
    against production: a PATCH whose filter matches nothing returns ``204`` with
    an empty body, and so does one that wrote three rows. ``curl --fail-with-body``
    trips on neither, which is why a sibling script's "another run took
    it" branch only ever fires on a transport error. Asking for
    ``Prefer: return=representation`` makes the same call return ``200`` and a
    JSON array, empty when nothing matched, so the lost race becomes visible.
    """


@dataclass(frozen=True)
class Claim:
    """The outcome of one slice's attempt to take the run.

    Not an exception, because the two ways of losing are opposites. ``claimed``
    false with ``budget_exhausted`` false means a sibling slice got there first,
    which is normal and means exit cleanly (G-06 calls it "absorbed, not
    duplicated"). ``budget_exhausted`` true means the run is still RUNNING and
    nobody will ever come back for it, which is a condition a stale detector has
    to surface -- so it must not be swallowed by the same branch. The wrapper
    already reads that distinction out of the RPC; the caller decides what to do
    with each.
    """

    claimed: bool
    slice_count: int
    budget_exhausted: bool
    run: dict[str, Any] | None = None


@dataclass(frozen=True)
class StepAdvance:
    """Whether ``current_step`` moved. ``advanced`` false means the run is not
    RUNNING or the step did not increase -- neither is an error, and neither may
    be reported as one."""

    advanced: bool
    current_step: int | None = None


@dataclass(frozen=True)
class Page:
    """One page of rows plus the row count the server is willing to stand behind.

    ``total`` is ``None`` rather than a guess when the server answers
    ``Content-Range: 0-999/*``, which is what it does without ``count=exact``. A
    page with an unknown length cannot be paged safely, and the caller is told so.
    """

    rows: list[dict[str, Any]]
    total: int | None


# ── helpers ─────────────────────────────────────────────────────────────────


_CONTENT_RANGE_TOTAL = re.compile(r"/(\d+)\s*$")


def _content_range_total(header: str) -> int | None:
    """The ``/N`` of a ``Content-Range: 0-999/1337`` header, or ``None`` for ``*``.

    ``*`` means "I am not counting". Returning ``None`` and letting the caller
    refuse is the whole point: the alternative -- treating ``*`` as "probably the
    end" -- is how a read stops one page early and nobody notices.
    """
    if not header:
        return None
    match = _CONTENT_RANGE_TOTAL.search(header.strip())
    return int(match.group(1)) if match else None


def _chunks(rows: Sequence[Any], size: int) -> Iterator[list[Any]]:
    """Split a sequence into fixed-size lists.

    Never a ``set``: CPython salts string hashes per process, and a batch order
    that moves between two runs of the same code is a batch that may be cut in a
    different place each time, which is replay safety by accident instead of by
    construction (R-036).
    """
    for start in range(0, len(rows), size):
        yield list(rows[start : start + size])


def _require(payload: Any, function: str, *keys: str) -> dict[str, Any]:
    """Confirm the RPC returned the object shape this module was written against.

    The alternative is returning a dict and letting the caller write
    ``result.get("marked", 0)``, which is a default that silently converts a
    schema change into a plausible wrong number. A missing key here is a loud
    failure at the boundary, which is where it is cheap to notice.
    """
    if not isinstance(payload, dict):
        raise DatabaseRefused(
            f"{function} returned {type(payload).__name__}, expected a jsonb object"
        )
    missing = [key for key in keys if key not in payload]
    if missing:
        raise DatabaseRefused(
            f"{function} returned an object without {', '.join(sorted(missing))}; "
            "the wrapper changed shape and this client has not been taught it"
        )
    return payload


def _as_int(value: Any, function: str) -> int:
    """PostgREST returns a bare scalar for a scalar-returning function, so
    ``bigint`` arrives as a JSON number and `run_event`'s event id may or may not
    arrive quoted depending on the version. Accept both, refuse anything else."""
    if isinstance(value, bool):
        raise DatabaseRefused(f"{function} returned a boolean where an integer was declared")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DatabaseRefused(
            f"{function} returned {value!r}, which is not an integer"
        ) from exc


def _table(name: str) -> str:
    if name not in TABLE_NAMES:
        raise IdentifierRejected(
            f"{name!r} is not a table this client may touch; allowed: "
            f"{', '.join(sorted(TABLE_NAMES))}. The runner's own rows go through "
            "the agent_catalog wrappers, which PostgREST cannot bypass."
        )
    return name


def _identifier(name: str, role: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER.match(name):
        raise IdentifierRejected(f"{role} {name!r} is not a bare lowercase column name")
    return name


def _filter(column: str, value: Any) -> str:
    """Build one ``col=eq.value`` pair.

    The value is percent-encoded rather than validated, because a filter value
    can legitimately be an ``item_key`` (which embeds a provider id) or free text,
    and refusing those would push the encoding problem back onto every caller.
    Encoding is the right answer; the risk is that unencoded text could name a
    second filter pair, and quoting removes it.
    """
    safe = _identifier(column, "filter column")
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif value is None:
        text = "null"
    else:
        text = urllib.parse.quote(str(value), safe="")
    return f"{safe}=eq.{text}"


def _filter_in(column: str, values: Sequence[Any]) -> str:
    """Build one ``col=in.("a","b")`` pair over a bounded list of literals.

    ``in`` and not a caller-built ``eq``, for the same reason ``eq`` is the only
    operator at all: this file must not be able to express an arbitrary query. What
    a snapshot genuinely cannot do without is "these hundred product ids" -- one
    round trip per product would be 206 calls and about two minutes of a 540 second
    budget spent reading, and the alternative that *would* be fast
    (``service_execute_sql`` with a built string) is the corruption filed as
    a separately filed bug.

    Every element is a JSON-quoted, percent-encoded literal, so a value cannot
    close the parenthesis, open a second operator, or name a column. Confirmed
    live: ``product_id=in.("<uuid>","<uuid>")`` is what PostgREST answers, and a
    bare unquoted uuid list is a 400.
    """
    safe = _identifier(column, "filter column")
    if not values:
        raise ValueError(f"in() filter on {safe} got no values; use eq for the empty case")
    if len(values) > IN_FILTER_CHUNK:
        raise BatchRejected(
            f"in() filter on {safe} got {len(values)} values; this client caps the list "
            f"at {IN_FILTER_CHUNK} so one URL cannot grow without bound"
        )
    # Each value becomes a double-quoted PostgREST literal (backslash and quote
    # escaped), and the whole list is then percent-encoded exactly once. Encoding the
    # elements first and the list again would turn every ``%`` into ``%25``.
    rendered = ",".join(
        '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"' for value in values
    )
    quoted = urllib.parse.quote("(" + rendered + ")", safe="(),")
    return f"{safe}=in.{quoted}"


def _order(column: str, direction: str) -> str:
    """Build one ``order=col.asc`` term.

    Paging without ordering is not a slow read, it is a wrong one. PostgREST makes
    no ordering promise without ``ORDER BY``, so an offset walk over an unordered
    query can return the same row twice and skip another, and the snapshot would
    carry a product twice and lose a ficha -- with a row count that still adds up.
    That is why ``select_all`` refuses to page without an order rather than
    defaulting to one: the caller names the key it is paging by.
    """
    safe = _identifier(column, "order column")
    if direction not in ("asc", "desc"):
        raise IdentifierRejected(f"sort direction {direction!r} is not 'asc' or 'desc'")
    return f"order={safe}.{direction}"


def new_uuid() -> str:
    """Mint a row id on this side of the wire.

    Required for two different reasons. The write path returns no generated ids,
    so a value that is needed later has to exist before the call. And
    `agent_catalog_apply_products` compares each ``product_details.id`` against
    the products that already exist to refuse stealing another product's detail
    row -- that check needs ids to compare, which means the caller has to have
    chosen them rather than discovered them.
    """
    return str(uuid.uuid4())


def iso_instant(moment: datetime) -> str:
    """Render an aware datetime the way the wrappers cast it.

    They cast with ``(e->>'observed_at')::timestamptz``. Postgres reads a naive
    string in the *session* zone, which on Supabase is UTC -- so a naive local
    wall clock would silently shift by the run's own ``effective_tz`` offset and
    R-021's filter would exclude the wrong population. Requiring an aware value
    turns that from a quiet data bug into an error.
    """
    if moment.tzinfo is None:
        raise ValueError(
            "observed_at must be timezone-aware; a naive local time would be read "
            "as UTC and shifted against the run's effective_tz (G-14)"
        )
    return moment.astimezone(timezone.utc).isoformat()


def _is_loopback(base: str) -> bool:
    """Plain http is accepted for a database on this machine and nowhere else."""
    host = urllib.parse.urlsplit(base).hostname or ""
    return base.startswith("http://") and host in ("127.0.0.1", "localhost", "::1")


def credentials_present(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the deterministic step's environment is complete.

    The extraction step's assertion is the inverse of this and belongs in shell
    (``env | grep -c '^SUPABASE_'`` must be zero); this one exists so the
    deterministic step can refuse to start at all rather than failing on its
    first write with a 401.
    """
    env = os.environ if environ is None else environ
    return bool(env.get(ENV_URL, "").strip()) and bool(
        (env.get(ENV_SERVICE_ROLE, "").strip() or env.get(ENV_SERVICE_KEY, "").strip())
    )


# ── the client ──────────────────────────────────────────────────────────────


class Client:
    """A PostgREST client bound to one project and one role.

    The credential is an instance attribute rather than a module global on
    purpose. the legacy script reads its key at import time, which is what
    lets that file be imported by something that was never meant to have the key
    in its process at all -- the failure mode §7.1 exists to rule out. Here the
    module imports cleanly with no environment at all, and the key exists only
    for as long as a caller that already had it holds a ``Client``.

    ``__repr__`` omits the key so an accidental interpolation of the object into
    an exception message, a traceback frame or a debug print cannot leak it.
    """

    __slots__ = ("_base", "_key", "_timeout", "_rest")

    def __init__(self, base: str, key: str, timeout: int = REQUEST_TIMEOUT_SECONDS,
                 rest_prefix: str = "/rest/v1") -> None:
        self._base = base.rstrip("/")
        self._key = key
        self._timeout = timeout
        self._rest = "/" + rest_prefix.strip("/") if rest_prefix.strip("/") else ""

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"<Client {self._base} role=service_role key=<withheld>>"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Client":
        """Build the client from the environment, or refuse.

        No URL default, no key default, no key file. A client pointed at the
        wrong project is worse than no client: it would write a real catalog run
        somewhere the owner is not looking.
        """
        env = os.environ if environ is None else environ
        base = env.get(ENV_URL, "").strip()
        key = env.get(ENV_SERVICE_ROLE, "").strip() or env.get(ENV_SERVICE_KEY, "").strip()
        if not base or not key:
            raise CredentialsAbsent(
                "no Supabase credentials in this environment (looked for "
                f"{ENV_URL} and {ENV_SERVICE_ROLE}/{ENV_SERVICE_KEY}). This module "
                "belongs to the deterministic step; the extraction step is supposed "
                "to have none, and failing here is how that stays true."
            )
        if not base.startswith("https://") and not _is_loopback(base):
            raise CredentialsAbsent(f"{ENV_URL} is not an https URL; refusing to talk to {base!r}")
        return cls(base, key, rest_prefix=env.get(ENV_REST_PREFIX, "/rest/v1"))

    # ── transport ──

    def _request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        prefer: str = "",
        attempts: int = 1,
        with_headers: bool = False,
    ) -> Any:
        """One HTTP call. Returns parsed JSON, or ``None`` for an empty body.

        ``with_headers`` returns ``(body, headers)`` instead. It exists for the one
        answer the body cannot give: how many rows the server thinks there are. A
        paged read that trusts its own ``limit`` is wrong the moment the server's
        cap is lower than the page size, and PostgREST's is -- measured live at
        1000 rows against a 1337-row table on 2026-10-09. ``Content-Range`` with
        ``Prefer: count=exact`` turns that from a silent truncation into a number
        the caller can check itself against.

        Retries are opt-in and default to one attempt because most of what this
        module calls is unsafe to repeat blind. ``agent_catalog_run_event``
        appends a row to a table that exists to be an ordered ledger, and
        ``agent_catalog_claim_run`` increments ``slice_count`` -- retrying either
        after a timeout duplicates work the run would then have to notice and
        reconcile. A caller that knows its call is idempotent (a
        ``checkpoint_items`` upsert, a GET) can ask for more attempts; a caller
        that is not sure should not, and should let the slice die and be resumed,
        which is what resume is for.
        """
        body = None
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
        }
        if payload is not None:
            # ensure_ascii=False keeps non-ASCII on the wire as itself. Postgres
            # stores the same characters either way, but a corpus full of accents
            # and an emoji in a name should not have to survive a JSON escape
            # round trip to get there.
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        url = self._base + path

        last: DatabaseRefused | None = None
        for _ in range(max(1, attempts)):
            request = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    received = dict(response.headers)
                    raw = response.read().decode("utf-8", "replace")
                    if not raw.strip():
                        return (None, received) if with_headers else None
                    try:
                        parsed = json.loads(raw)
                    except ValueError as exc:
                        # A 200 that is not JSON is a proxy or a gateway, not
                        # PostgREST. Letting json's own message escape would name
                        # neither the URL nor the fact that a write may have landed.
                        raise DatabaseRefused(
                            f"{method} {path} answered {len(raw)} bytes that are not JSON; "
                            "the request may or may not have been applied"
                        ) from exc
                    return (parsed, received) if with_headers else parsed
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")[:MAX_ERROR_BODY_CHARS]
                # A 4xx is the database's answer and repeating it will not change
                # it; only 5xx is worth another attempt.
                if exc.code < 500:
                    raise _refusal(method, path, exc.code, raw) from None
                last = _refusal(method, path, exc.code, raw)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = DatabaseRefused(f"{method} {path} did not complete: {exc}")
        assert last is not None
        raise last

    def _rpc(
        self,
        function: str,
        payload: Any = None,
        prefer: str = "",
        attempts: int = 1,
    ) -> Any:
        if function not in RPC_FUNCTIONS:
            raise IdentifierRejected(
                f"{function!r} is not one of this runner's wrappers; allowed: "
                f"{', '.join(sorted(RPC_FUNCTIONS))}"
            )
        return self._request(
            "POST", f"{self._rest}/rpc/{function}", payload=payload, prefer=prefer, attempts=attempts
        )

    # ── direct table access ──
    #
    # Thin on purpose. The runner's own state is reachable only through the
    # wrappers, so what is left is the four domain tables the snapshot and the
    # upsert need.

    def select_page(
        self,
        table: str,
        *,
        eq: Mapping[str, Any] | None = None,
        contains: Mapping[str, Sequence[Any]] | None = None,
        columns: Sequence[str] | str = "*",
        order: tuple[str, str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Page:
        """Read one page of rows, and how many rows the server believes exist.

        Two filters and one sort, and no more. ``eq`` is a primary-key lookup and
        ``in`` is a bounded list of literals; a general query-string escape hatch
        here would be ``service_execute_sql`` wearing a different hat, because the
        moment a caller can assemble a filter out of supplier text, §7.3 is
        undefended. ``order`` is not a filter and exists because paging without it
        is a wrong answer rather than a slow one.

        ``total`` comes from ``Content-Range`` under ``Prefer: count=exact`` and is
        ``None`` when the server declines to say. A caller that pages on it
        (``select_all``) treats ``None`` as a refusal to guess and raises, because
        the alternative is a loop that stops early and reports a catalog smaller
        than it is.
        """
        name = _table(table)
        chosen = (columns,) if isinstance(columns, str) else tuple(columns)
        select_clause = (
            "*" if chosen == ("*",) else ",".join(_identifier(c, "column") for c in chosen)
        )
        parts = [f"select={select_clause}"]
        for column, value in (eq or {}).items():
            parts.append(_filter(column, value))
        for column, values in (contains or {}).items():
            parts.append(_filter_in(column, values))
        if order is not None:
            parts.append(_order(order[0], order[1]))
        if limit is not None:
            parts.append(f"limit={int(limit)}")
        if offset is not None:
            parts.append(f"offset={int(offset)}")
        path = f"{self._rest}/{name}?" + "&".join(parts)
        payload, headers = self._request("GET", path, prefer="count=exact", with_headers=True)
        rows = list(payload) if isinstance(payload, list) else []
        return Page(rows=rows, total=_content_range_total(headers.get("Content-Range", "")))

    def select(
        self,
        table: str,
        *,
        eq: Mapping[str, Any] | None = None,
        contains: Mapping[str, Sequence[Any]] | None = None,
        columns: Sequence[str] | str = "*",
        order: tuple[str, str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """One page, without the total. See ``select_page`` for what the parameters
        can and cannot express."""
        return self.select_page(
            table, eq=eq, contains=contains, columns=columns,
            order=order, limit=limit, offset=offset,
        ).rows

    def select_all(
        self,
        table: str,
        *,
        eq: Mapping[str, Any] | None = None,
        contains: Mapping[str, Sequence[Any]] | None = None,
        columns: Sequence[str] | str = "*",
        order: tuple[str, str] | None = None,
        page: int = PAGE_ROWS,
    ) -> list[dict[str, Any]]:
        """Every row matching the filter, walked by offset over a stated order.

        Three refusals, each for a reason that has already cost something:

        * **no ``order``** -- an unordered offset walk can repeat a row and skip
          another between pages, and both look fine in a row count;
        * **``page`` above ``PAGE_ROWS``** -- the same number the server caps at, so
          a page size above the cap reads as a full page and stops the walk early;
        * **a server that will not report a total** -- without one there is no way
          to tell "the last page" from "where the cap cut us off".

        The third is the expensive one and it is why ``select_page`` asks for
        ``count=exact``. Supabase answers it; a self-hosted PostgREST without
        ``db-max-rows`` configured can answer ``*``, and this raises rather than
        guesses. Guessing is what the legacy snapshot did when it trusted a
        ``limit``.
        """
        if order is None:
            raise ValueError(
                "select_all needs order=(column, 'asc'|'desc'); paging an unordered "
                "query can repeat a row and skip another between pages"
            )
        if page > PAGE_ROWS:
            raise ValueError(
                f"select_all page={page} is above the server cap of {PAGE_ROWS}; a page "
                "the server silently shortens ends the walk early and looks complete"
            )
        rows: list[dict[str, Any]] = []
        total: int | None = None
        offset = 0
        while True:
            chunk = self.select_page(
                table, eq=eq, contains=contains, columns=columns,
                order=order, limit=page, offset=offset,
            )
            if chunk.total is None:
                raise DatabaseRefused(
                    f"{table} answered Content-Range without a total even under "
                    "Prefer: count=exact; this client will not page a read whose "
                    "length it cannot verify",
                    code="COUNT_UNAVAILABLE",
                )
            if total is None:
                total = chunk.total
            elif total != chunk.total:
                # The table moved under the walk. For a snapshot taken at run start
                # that is exactly the case to refuse: a product inserted by the
                # owner halfway through would otherwise enter the artifact with no
                # note that the run compared against two different catalogs.
                raise DatabaseRefused(
                    f"{table} reported {total} rows on the first page and {chunk.total} "
                    "partway through the walk; the table changed while it was being "
                    "read, and a snapshot is only meaningful at one instant",
                    code="ROW_COUNT_MOVED",
                )
            rows.extend(chunk.rows)
            offset += len(chunk.rows)
            if not chunk.rows or offset >= total:
                break
        if total is not None and len(rows) != total:
            raise DatabaseRefused(
                f"{table} walk collected {len(rows)} of {total} rows",
                code="PAGED_READ_SHORT",
            )
        return rows

    def compare_and_set(
        self,
        table: str,
        *,
        eq: Mapping[str, Any],
        changes: Mapping[str, Any],
        expect: int = 1,
        attempts: int = 1,
    ) -> list[dict[str, Any]]:
        """A conditional write whose row count is part of the answer.

        ``PATCH ... ?col=eq.<value>`` with ``Prefer: return=representation``.
        The row count is not a diagnostic: it is the whole point. Without the
        Prefer header a zero-row PATCH and a three-row PATCH are both ``204`` with
        an empty body, which is precisely why the shipped claim check cannot see
        a lost race. With it, zero rows arrives as ``200 []`` and
        ``expect=1`` turns that into a ``ClaimLost`` the slice can act on.

        Safe to retry: the same guarded write applied twice writes twice.
        """
        name = _table(table)
        if not eq:
            raise ValueError("compare_and_set needs at least one equality filter")
        parts = [_filter(column, value) for column, value in eq.items()]
        path = f"{self._rest}/{name}?" + "&".join(parts)
        rows = self._request(
            "PATCH",
            path,
            payload=dict(changes),
            prefer="return=representation",
            attempts=attempts,
        )
        matched = list(rows) if isinstance(rows, list) else []
        if len(matched) != expect:
            raise ClaimLost(
                f"{name} compare-and-set matched {len(matched)} rows, expected {expect}; "
                "another slice or another run holds this row"
            )
        return matched

    def upsert(
        self,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        on_conflict: str = "id",
        attempts: int = 1,
    ) -> list[dict[str, Any]]:
        """Write up to ``MAX_BATCH_ROWS`` rows as one atomic statement.

        A JSON array with ``Prefer: resolution=merge-duplicates`` is PostgREST's
        ``INSERT ... ON CONFLICT DO UPDATE``: one round trip, one transaction,
        and no SQL assembled from any of the values. That is the whole reason to
        prefer it over the legacy write path -- 215 individual calls is about 130
        seconds of a 540 second budget against a measured 0.58-1.00 s round trip,
        where the same rows at 20 per call cost about 7.

        The duplicate-target check is not decoration. Postgres refuses
        ``ON CONFLICT DO UPDATE`` for a row the same statement already touched
        ("cannot affect row a second time"), so a batch that names the same
        conflict target twice fails with an error that names neither row. Catching
        it here turns that into a message naming the value.
        """
        name = _table(table)
        if not rows:
            raise BatchRejected("upsert called with no rows")
        if len(rows) > MAX_BATCH_ROWS:
            raise BatchRejected(
                f"upsert got {len(rows)} rows; the batch cap is {MAX_BATCH_ROWS}"
            )
        if on_conflict not in CONFLICT_TARGETS.get(name, ()):
            raise IdentifierRejected(
                f"{name} has no declared conflict target {on_conflict!r}; "
                f"declared: {', '.join(CONFLICT_TARGETS.get(name, ())) or 'none'}"
            )
        _reject_repeated_targets(rows, on_conflict, name)
        returned = self._request(
            "POST",
            f"{self._rest}/{name}?on_conflict={on_conflict}",
            payload=[dict(row) for row in rows],
            prefer="resolution=merge-duplicates,return=representation",
            attempts=attempts,
        )
        written = list(returned) if isinstance(returned, list) else []
        # ON CONFLICT DO UPDATE touches every input row, insert or update, so N in
        # means N out. Anything else means the statement did not run as written --
        # and a caller that only checked "the call returned" would call that a
        # success. Same reasoning as the CAS, one level up.
        if len(written) != len(rows):
            raise DatabaseRefused(
                f"{name} upsert sent {len(rows)} rows and got {len(written)} back; "
                "the write cannot be assumed to have landed",
                status=0,
                code="BATCH_ROUNDTRIP_SHORT",
            )
        return written

    # ── the runner's own wrappers ──

    def claim_run(self, run_id: str, attempts: int = 1) -> Claim:
        """Take the run for one slice, or learn why not.

        Retrying is refused outright. ``claim_run`` is a guarded
        ``slice_count = slice_count + 1 ... RETURNING``, so a retry after a
        response that never arrived would spend a second slice of the run's
        fifteen on a slice that already holds one. Losing the race is cheap; the
        bound is not.

        The compare-and-set lives inside the RPC rather than in a REST filter,
        which is strictly better than the shape at a plain conditional PATCH:
        the guard and the increment are one statement, so they cannot be split by
        a crash, and the answer names which of the two ways of losing occurred
        instead of collapsing both into an empty array.
        """
        if attempts != 1:
            raise ValueError(
                "claim_run is not retryable: it increments slice_count, and a "
                "retry after a lost response spends another slice of the run's bound"
            )
        payload = _require(
            self._rpc("agent_catalog_claim_run", {"p_run_id": run_id}),
            "agent_catalog_claim_run",
            "claimed",
            "slice_count",
            "budget_exhausted",
        )
        return Claim(
            claimed=bool(payload["claimed"]),
            slice_count=_as_int(payload["slice_count"], "agent_catalog_claim_run"),
            budget_exhausted=bool(payload["budget_exhausted"]),
            run=payload.get("run") if isinstance(payload.get("run"), dict) else None,
        )

    def claim_items(self, run_id: str, stage: int, limit: int = MAX_BATCH_ROWS) -> list[dict[str, Any]]:
        """Take up to ``limit`` items at ``stage`` into ``IN_FLIGHT``.

        ``FOR UPDATE SKIP LOCKED`` inside the wrapper means a sibling slice gets
        the next rows instead of blocking on ours, so two overlapping slices
        partition the work rather than colliding on it.
        """
        rows = self._rpc(
            "agent_catalog_claim_items",
            {"p_run_id": run_id, "p_stage": int(stage), "p_limit": int(limit)},
        )
        return list(rows) if isinstance(rows, list) else []

    def checkpoint_items(self, run_id: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        """Write one checkpoint per product (R-030, R-031).

        Split at ``MAX_BATCH_ROWS`` and summed. Chunking costs nothing in
        atomicity because each PostgREST request is already its own transaction,
        and a cut between chunks is exactly the case resume exists for: the
        stamped items are found and promoted, the untouched ones are re-done.
        """
        return _sum_batches(
            rows,
            "agent_catalog_checkpoint_items",
            lambda chunk: self._rpc(
                "agent_catalog_checkpoint_items", {"p_run_id": run_id, "p_rows": chunk}
            ),
            ("requested", "inserted", "updated"),
        )

    def assert_provenance(self, run_id: str, items: Sequence[Mapping[str, Any]]) -> dict:
        """Refuse a payload that leaks one provider's terms onto another's (R-027).

        The enforcement is the wrapper's, not this one's -- three separate
        checks, including an untagged extracted field, which is a field with no
        provenance rather than a field with incomplete data. What this client
        guarantees is only that the call is a call and not an interpretation of
        the payload it carries.
        """
        return _sum_batches(
            items,
            "agent_catalog_assert_provenance",
            lambda chunk: self._rpc(
                "agent_catalog_assert_provenance", {"p_run_id": run_id, "p_items": chunk}
            ),
            ("checked", "declared_providers", "ok"),
        )

    def apply_products(self, run_id: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Leg one of three: write the products.

        Called with its siblings and never instead of them. Leg two stamps what
        leg one wrote and leg three marks it done, each in its own transaction,
        because a cut between the second and the third has to leave something the
        next slice can recognise -- that is the whole content of R-031.

        ``action='new'`` raises ``CATALOG_INSERT_FORBIDDEN`` inside the wrapper:
        a product with no counterpart is returned to the client, never inserted
        (R-011). This client does not second-guess that; it does refuse to chunk
        a batch whose ``new`` rows would be written before the ``update`` rows it
        has not, so the forbidden row is the first thing the wrapper sees rather
        than the twenty-first.
        """
        _reject_new_action(rows)
        return _sum_batches(
            rows,
            "agent_catalog_apply_products",
            lambda chunk: self._rpc(
                "agent_catalog_apply_products", {"p_run_id": run_id, "p_rows": chunk}
            ),
            ("requested", "applied", "business_id"),
        )

    def stamp_applies(self, run_id: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        """Leg two: record which product each item landed on.

        This is the row that makes replay safe, and it is a separate statement
        from leg one on purpose. The wrapper keeps ``applied_at`` when the
        product id is unchanged, so re-stamping the same item does not move the
        timestamp that says when the write happened.
        """
        return _sum_batches(
            rows,
            "agent_catalog_stamp_applies",
            lambda chunk: self._rpc(
                "agent_catalog_stamp_applies", {"p_run_id": run_id, "p_rows": chunk}
            ),
            ("requested", "stamped", "skipped_already_done"),
        )

    def mark_done(self, run_id: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        """Leg three, last: the item is ``DONE``.

        The table CHECK makes ``DONE`` without a stamp unpersistable for anything
        but ``action='new'``, so this call cannot succeed on an item that was
        never written. That is why the order is three statements rather than one
        convenient transaction: the constraint is the enforcement, and a single
        merged statement would have nothing left to enforce.
        """
        return _sum_batches(
            rows,
            "agent_catalog_mark_done",
            lambda chunk: self._rpc(
                "agent_catalog_mark_done", {"p_run_id": run_id, "p_rows": chunk}
            ),
            ("requested", "marked", "unchanged"),
        )

    def run_event(self, run_id: str, event: str, detail: Mapping[str, Any] | None = None) -> int:
        """Append one row to the run's ledger and return its id.

        Not retryable, and not chunked: a ledger row is an event, not a
        quantity. Two rows saying ``ITEM_DONE`` for the same item describe
        something that did not happen.

        Step advance and event insert stay separate calls (R-008): the owner
        screen reads the ledger to explain where a run stopped, and a step
        recorded as advanced with no explanation beside it is worse than either
        half alone.
        """
        if event not in RUN_EVENTS:
            raise ValueError(
                f"{event!r} is not in run_events.event; the live CHECK lists "
                f"{len(RUN_EVENTS)} values"
            )
        return _as_int(
            self._rpc(
                "agent_catalog_run_event",
                {
                    "p_run_id": run_id,
                    "p_event": event,
                    "p_detail": dict(detail) if detail is not None else None,
                },
            ),
            "agent_catalog_run_event",
        )

    def run_step_done(self, run_id: str, step: int) -> StepAdvance:
        """Advance ``current_step``, if the run is still RUNNING and the step moves.

        The wrapper only moves the step forward, so re-announcing the current
        step is a no-op rather than a regression.
        """
        payload = _require(
            self._rpc("agent_catalog_run_step_done", {"p_run_id": run_id, "p_step": int(step)}),
            "agent_catalog_run_step_done",
            "advanced",
        )
        current = payload.get("current_step")
        return StepAdvance(
            advanced=bool(payload["advanced"]),
            current_step=_as_int(current, "agent_catalog_run_step_done") if current else None,
        )


    # ── reading the run back ──

    def read_run(self, run_id: str) -> dict[str, Any]:
        """The run row and its item counts, without claiming anything.

        ``claim_run`` only returns the row when the claim succeeds. A slice that did not
        get the run still has to say why, and a slice that did has to know what stage it
        woke up in; both read this.
        """
        payload = _require(
            self._rpc("agent_catalog_read_run", {"p_run_id": run_id}, attempts=3),
            "agent_catalog_read_run", "run", "counts",
        )
        if not isinstance(payload["run"], dict):
            raise DatabaseRefused("agent_catalog_read_run returned no run object")
        return payload

    def read_items(self, run_id: str, stages: Sequence[int] | None = None,
                   page: int = 200) -> list[dict[str, Any]]:
        """Every item of the run at the given stages, walked by key.

        Keyset paging: the cursor is the last ``item_key`` seen, so an item that changes
        stage between two pages neither shifts the rest nor shows up twice.
        """
        rows: list[dict[str, Any]] = []
        after = ""
        while True:
            chunk = self._rpc(
                "agent_catalog_read_items",
                {"p_run_id": run_id,
                 "p_stages": None if stages is None else [int(s) for s in stages],
                 "p_after": after, "p_limit": int(page)},
                attempts=3,
            )
            if not isinstance(chunk, list):
                raise DatabaseRefused("agent_catalog_read_items did not return a list")
            if not chunk:
                break
            rows.extend(chunk)
            after = str(chunk[-1]["item_key"])
            if len(chunk) < page:
                break
        return rows

    def save_pairs(self, run_id: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        """Persist the model's verdicts, one row per pair. An upsert: safe to repeat."""
        return _sum_batches(
            rows, "agent_catalog_save_pairs",
            lambda chunk: self._rpc(
                "agent_catalog_save_pairs", {"p_run_id": run_id, "p_rows": chunk}, attempts=3),
            ("requested", "saved"),
        )

    def read_pairs(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._rpc("agent_catalog_read_pairs", {"p_run_id": run_id}, attempts=3)
        return list(rows) if isinstance(rows, list) else []

    def advance_items(self, run_id: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        """Move items forward and merge a patch into their payload.

        Unlike ``checkpoint_items`` this never replaces a payload and never moves a
        stage backwards, so repeating it after a cut converges instead of undoing.
        """
        return _sum_batches(
            rows, "agent_catalog_advance_items",
            lambda chunk: self._rpc(
                "agent_catalog_advance_items", {"p_run_id": run_id, "p_rows": chunk}, attempts=3),
            ("requested", "advanced"),
        )

    def run_progress(self, run_id: str, result: Mapping[str, Any]) -> bool:
        """Merge one stage's numbers into ``runs.result``. A key-wise merge: repeatable."""
        payload = _require(
            self._rpc("agent_catalog_run_progress",
                      {"p_run_id": run_id, "p_result": dict(result)}, attempts=3),
            "agent_catalog_run_progress", "updated",
        )
        return bool(payload["updated"])

    def run_finish(self, run_id: str, status: str,
                   result: Mapping[str, Any] | None = None) -> bool:
        """Leave ``RUNNING``. Returns whether this call was the one that did it.

        Not retried: the wrapper writes the closing event in the same statement, and a
        second attempt after a lost response finds the run already closed and reports
        ``False`` -- which is the truth, and harmless.
        """
        if status not in ("AWAITING_APPROVAL", "COMPLETED", "FAILED"):
            raise ValueError(f"{status!r} is not a status the runner may set")
        payload = _require(
            self._rpc("agent_catalog_run_finish",
                      {"p_run_id": run_id, "p_status": status,
                       "p_result": None if result is None else dict(result)}),
            "agent_catalog_run_finish", "finished",
        )
        return bool(payload["finished"])

    def auto_approve(self, run_id: str) -> dict[str, Any]:
        """Approve an automatic run on the agent's own authority (R-029)."""
        return _require(
            self._rpc("agent_catalog_auto_approve", {"p_run_id": run_id}),
            "agent_catalog_auto_approve", "to_apply", "returned_to_client",
        )

    def chosen_model(self) -> str:
        """The model the owner picked for this agent, as a bare identifier.

        The wrapper only ever returns something shaped like an identifier, and it is
        checked again here: this string becomes one argument of a command.
        """
        value = self._rpc("agent_catalog_model", {}, attempts=3)
        if not isinstance(value, str) or not _MODEL_ID.match(value):
            raise DatabaseRefused("agent_catalog_model did not return a model identifier")
        return value

    # ── private storage ──
    #
    # Rules, the prompt and the uploaded chats live in private buckets. They are read
    # with the same key the wrappers use, by the same half of the run, and nothing here
    # is reachable from the half that reads supplier text.

    def _storage(self, method: str, bucket: str, path: str, allowed: frozenset[str],
                 data: bytes | None = None, content_type: str = "application/octet-stream",
                 attempts: int = 3) -> bytes:
        if bucket not in allowed:
            raise IdentifierRejected(
                f"bucket {bucket!r} is not one this runner may {method.lower()}")
        if ".." in path or not _OBJECT_PATH.match(path):
            raise IdentifierRejected(f"object path {path!r} is not a path this runner builds")
        url = f"{self._base}/storage/v1/object/{bucket}/{urllib.parse.quote(path)}"
        headers = {"apikey": self._key, "Authorization": f"Bearer {self._key}"}
        if data is not None:
            headers["Content-Type"] = content_type
            headers["x-upsert"] = "true"
        last: DatabaseRefused | None = None
        for _ in range(max(1, attempts)):
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self._timeout * 2) as response:
                    body = response.read(MAX_OBJECT_BYTES + 1)
                    if len(body) > MAX_OBJECT_BYTES:
                        raise DatabaseRefused(
                            f"{bucket}/{path} is larger than {MAX_OBJECT_BYTES} bytes",
                            code="OBJECT_TOO_LARGE")
                    return body
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")[:MAX_ERROR_BODY_CHARS]
                refusal = DatabaseRefused(
                    f"{method} storage {bucket}/{path} -> {exc.code}",
                    status=exc.code, code="STORAGE_REFUSED", detail=raw)
                if exc.code < 500:
                    raise refusal from None
                last = refusal
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = DatabaseRefused(f"{method} storage {bucket}/{path} did not complete: {exc}")
        assert last is not None
        raise last

    def storage_get(self, bucket: str, path: str) -> bytes:
        """Download one private object. Idempotent, so it retries."""
        return self._storage("GET", bucket, path, STORAGE_READ_BUCKETS)

    def storage_put(self, bucket: str, path: str, data: bytes,
                    content_type: str = "application/json") -> None:
        """Write one object under the run's own folder, replacing what was there."""
        self._storage("POST", bucket, path, STORAGE_WRITE_BUCKETS, data=data,
                      content_type=content_type)


# ── batch helpers ───────────────────────────────────────────────────────────


def _sum_batches(
    rows: Sequence[Mapping[str, Any]],
    function: str,
    call: Callable[[list[Any]], Any],
    required: tuple[str, ...],
) -> dict[str, Any]:
    """Run a wrapper over ``rows`` in capped chunks and merge the answers.

    Counters add up. A member of ``required`` that is not a number -- ``ok`` from
    ``assert_provenance``, for instance -- cannot be summed, so every chunk has
    to agree on it; a chunk that disagrees means the wrapper is not reporting one
    run's outcome, which is a loud failure rather than a value taken from whichever
    chunk happened to be last. The declared return shape is per call and merging
    it is this function's whole responsibility, so if a wrapper ever starts
    returning something a merge cannot describe, this is the line to change.
    """
    if not rows:
        raise BatchRejected(f"{function} called with no rows; assert_batch would raise BATCH_EMPTY")
    totals: dict[str, Any] = {}
    for chunk in _chunks(rows, MAX_BATCH_ROWS):
        payload = _require(call(chunk), function, *required)
        for key in required:
            value = payload[key]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
            elif key in totals and totals[key] != value:
                raise DatabaseRefused(
                    f"{function} reported two different {key} across the chunks of "
                    f"one batch ({totals[key]!r} then {value!r}); refusing to merge "
                    "them into one answer"
                )
            else:
                totals[key] = value
    return totals


def _reject_repeated_targets(rows: Sequence[Mapping[str, Any]], column: str, table: str) -> None:
    """Refuse a batch naming the same conflict target twice.

    Postgres cannot apply ``ON CONFLICT DO UPDATE`` to a row the same statement
    already updated and answers "command cannot affect row a second time", which
    identifies neither row. Naming the offending value here costs one pass over
    at most twenty dicts.
    """
    seen: dict[Any, int] = {}
    for index, row in enumerate(rows):
        value = row.get(column)
        if value is None:
            continue
        if value in seen:
            raise BatchRejected(
                f"{table}.{column} {value!r} appears twice in one batch (rows "
                f"{seen[value]} and {index}); Postgres would refuse the whole statement "
                "with an error naming neither"
            )
        seen[value] = index


def _reject_new_action(rows: Sequence[Mapping[str, Any]]) -> None:
    """Refuse a batch containing ``action='new'`` before it is chunked.

    R-011 says a product with no counterpart is returned to the client and never
    inserted, and the wrapper enforces it. Enforcing it here as well is about
    ordering: a batch of twenty-five with two ``new`` rows at the end would
    already have written the other twenty-three before the refusal arrives.
    Catching it locally turns a partial write into none.
    """
    offenders = [str(row.get("item_key", "?")) for row in rows if row.get("action") == "new"]
    if offenders:
        raise InsertForbidden(
            "R-011: no product with action='new' may be written to products; "
            f"{len(offenders)} such item(s) in this batch: {', '.join(sorted(offenders)[:5])}"
            + (" ..." if len(offenders) > 5 else "")
        )


def _refusal(method: str, path: str, status: int, raw: str) -> DatabaseRefused:
    """Turn a PostgREST error body into an exception carrying its code.

    The code is what makes these failures branchable. ``ITEM_IDENTITY_FROZEN``
    means the caller tried to change a frozen input; ``CATALOG_INSERT_FORBIDDEN``
    means step 3's decision was not respected; ``PROVENANCE_VIOLATION`` means a
    provider's terms landed on another's product. Matching on the message text
    instead would make every one of those decisions hostage to a rewording.
    """
    code = ""
    detail = ""
    try:
        parsed = json.loads(raw) if raw.strip() else {}
        if isinstance(parsed, dict):
            code = str(parsed.get("code") or "")
            detail = str(parsed.get("detail") or "")
            message = str(parsed.get("message") or "")
    except ValueError:
        message = ""
    summary = message or (raw[:200] if raw else f"HTTP {status}")
    return DatabaseRefused(
        f"{method} {path} -> {status} {code or 'no code'}: {summary}".strip(),
        status=status,
        code=code,
        detail=detail,
    )