"""The gate. It reads exactly the rows that are about to be written and says yes or no.

There is no repair path here and there must not be one. A validator that fixes what it
does not like is a validator whose verdict nobody can act on. Every finding is a refusal
with a reason a person can go and look at, and a refused row is not written -- the rest
of the batch is (R-008: one failure does not take the rest of the work down with it).

Two things this module is not allowed to do:

* It does not **embed the app's rules**. Length limits, the icon set and the valid
  commission types arrive at run time in the ruleset (R-020) and are an argument here,
  never a constant. A missing ruleset is a refusal, not a pass: "I had nothing to check
  against" is not "nothing is wrong".
* It does not trust anything upstream to have behaved. The proposal was built by code
  in this same package and is checked anyway, because the row may have sat in the
  database for a day waiting for approval and what is checked here is what is sent.

The contract it enforces is the narrow one this agent has (see ``proposal``): an update
moves the price, the supplier, the commission and the supplier's own commercial terms.
The database function behind it would accept more columns; this refuses them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: The product columns this agent writes. Anything else in a row is refused here even
#: though the database would take it: an agent that only moves offers has no business
#: sending a name or a description, and if it ever does, something upstream is wrong.
WRITABLE_PRODUCT_KEYS = frozenset({"base_price", "provider_public_key"})

WRITING_ACTIONS = frozenset({"update", "touch"})

ROW_KEYS = frozenset({"item_key", "product_id", "action", "product", "commission",
                      "commission_clear", "conditions_delete", "conditions_insert"})

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class GateRefused(Exception):
    """The gate could not run at all. Carries the reason."""


@dataclass(frozen=True)
class Finding:
    """One refusal, addressed to something a person can go and look at."""

    code: str
    where: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.where}: {self.detail}"

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "where": self.where, "detail": self.detail}


@dataclass(frozen=True)
class Verdict:
    """The gate's answer for a batch."""

    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.findings

    def refused_items(self) -> frozenset[str]:
        return frozenset(f.where for f in self.findings)

    def for_item(self, item_key: str) -> list[Finding]:
        return [f for f in self.findings if f.where == item_key]

    def __str__(self) -> str:
        if self.ok:
            return "gate: OK"
        return f"gate: REFUSED ({len(self.findings)})\n" + "\n".join(
            "  " + str(f) for f in self.findings)


@dataclass(frozen=True)
class Rules:
    """The slice of the runtime ruleset this gate consumes."""

    icons: frozenset[str]
    commission_types: frozenset[str]
    title_min: int
    title_max: int
    details_max: int
    notes_max: int
    commission_min: float

    @classmethod
    def from_ruleset(cls, ruleset: Mapping[str, Any] | None) -> "Rules":
        """Read the limits, or refuse. No default stands in for a missing rule."""
        if not isinstance(ruleset, Mapping):
            raise GateRefused("no hay ruleset: el gate no puede fingir que validó")
        enums = ruleset.get("enums")
        limits = ruleset.get("limits")
        if not isinstance(enums, Mapping) or not isinstance(limits, Mapping):
            raise GateRefused("el ruleset no trae `enums` y `limits`")

        def limit(path: str, key: str) -> Any:
            node = limits.get(path)
            if not isinstance(node, Mapping) or key not in node:
                raise GateRefused(f"el ruleset no declara {path}.{key}")
            return node[key]

        icons = enums.get("icons")
        types = enums.get("commission_types")
        if not isinstance(icons, list) or not icons:
            raise GateRefused("el ruleset no trae el catálogo de iconos")
        if not isinstance(types, list) or not types:
            raise GateRefused("el ruleset no trae los tipos de comisión")
        return cls(
            icons=frozenset(str(i) for i in icons),
            commission_types=frozenset(str(t) for t in types),
            title_min=int(limit("conditions[].title", "min_len")),
            title_max=int(limit("conditions[].title", "max_len")),
            details_max=int(limit("conditions[].details", "max_len")),
            notes_max=int(limit("commission.notes", "max_len")),
            commission_min=float(limit("commission.value", "minimum")),
        )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_row(row: Mapping[str, Any], rules: Rules, item_provider: str | None,
              run_providers: Iterable[str]) -> list[Finding]:
    """Every reason this one row may not be written."""
    where = str(row.get("item_key") or "<sin item_key>")
    found: list[Finding] = []

    def refuse(code: str, detail: str) -> None:
        found.append(Finding(code, where, detail))

    if not row.get("item_key"):
        refuse("row-field", "fila sin `item_key`")

    extra = sorted(set(row) - ROW_KEYS)
    if extra:
        refuse("row-unknown-key", f"claves fuera del contrato de escritura: {extra}")

    action = row.get("action")
    if action == "new":
        # R-011, refused here as well as in the database. The database is the last line;
        # this is the one that says so before a round trip is spent on it.
        refuse("no-creation", "action=new: un producto sin contraparte vuelve al cliente")
        return found
    if action not in WRITING_ACTIONS:
        refuse("row-action", f"`{action}` no es una acción escribible")
        return found

    product_id = row.get("product_id")
    if not isinstance(product_id, str) or not _UUID.match(product_id):
        refuse("row-target", f"acción `{action}` sin un `product_id` válido")

    if action == "touch":
        payload_keys = sorted(set(row) - {"item_key", "product_id", "action"})
        if payload_keys:
            refuse("touch-carries-data",
                   f"un refresco no escribe datos y esta fila trae {payload_keys}")
        return found

    product = row.get("product")
    if not isinstance(product, Mapping):
        refuse("row-product", "update sin objeto `product`")
        product = {}
    unknown = sorted(set(product) - WRITABLE_PRODUCT_KEYS)
    if unknown:
        refuse("product-unknown-key",
               f"este agente no escribe {unknown}; sólo {sorted(WRITABLE_PRODUCT_KEYS)}")
    if "base_price" in product:
        price = product["base_price"]
        if not _is_number(price) or price <= 0:
            refuse("price-invalid", f"precio no positivo: {price!r}")
    if "provider_public_key" in product:
        provider = product["provider_public_key"]
        if provider not in set(run_providers):
            refuse("provider-foreign", "el proveedor a escribir no es de esta corrida")
        elif item_provider is not None and provider != item_provider:
            # R-027 at its sharpest: the product would be published under a supplier
            # other than the one whose offer won it.
            refuse("provider-mismatch",
                   "el proveedor a escribir no es el que hizo la oferta ganadora")

    commission = row.get("commission")
    if commission is not None:
        if not isinstance(commission, Mapping):
            refuse("commission-shape", "`commission` no es un objeto")
        else:
            if commission.get("commission_type") not in rules.commission_types:
                refuse("commission-type",
                       f"tipo de comisión fuera del ruleset: {commission.get('commission_type')!r}")
            value = commission.get("value")
            if not _is_number(value) or value < rules.commission_min:
                refuse("commission-value", f"comisión inválida: {value!r}")
            notes = commission.get("notes")
            if notes is not None and (not isinstance(notes, str) or len(notes) > rules.notes_max):
                refuse("commission-notes", f"nota de comisión de más de {rules.notes_max} caracteres")
        if row.get("commission_clear"):
            refuse("commission-both", "la fila pide escribir y borrar la comisión a la vez")

    deletes = row.get("conditions_delete")
    if deletes is not None:
        if not isinstance(deletes, list) or not all(
                isinstance(d, str) and _UUID.match(d) for d in deletes):
            refuse("conditions-delete", "`conditions_delete` debe ser una lista de ids")

    inserts = row.get("conditions_insert")
    if inserts is not None:
        if not isinstance(inserts, list):
            refuse("conditions-insert", "`conditions_insert` no es una lista")
            inserts = []
        seen_ids: set[str] = set()
        for index, node in enumerate(inserts):
            label = f"conditions_insert[{index}]"
            if not isinstance(node, Mapping):
                refuse("conditions-insert", f"{label} no es un objeto")
                continue
            identifier = node.get("id")
            if not isinstance(identifier, str) or not _UUID.match(identifier):
                refuse("conditions-insert", f"{label} sin id determinista")
            elif identifier in seen_ids:
                refuse("conditions-insert", f"{label} repite un id")
            else:
                seen_ids.add(identifier)
            title = node.get("title")
            if not isinstance(title, str) or not (rules.title_min <= len(title) <= rules.title_max):
                refuse("condition-title",
                       f"{label}: título fuera de {rules.title_min}..{rules.title_max} caracteres")
            details = node.get("details")
            if details is not None and (
                    not isinstance(details, str) or len(details) > rules.details_max):
                refuse("condition-details",
                       f"{label}: detalle de más de {rules.details_max} caracteres")
            icon = node.get("icon")
            if icon is not None and icon not in rules.icons:
                refuse("condition-icon", f"{label}: icono fuera del ruleset ({icon!r})")

    return found


def gate(
    rows: Sequence[Mapping[str, Any]],
    *,
    ruleset: Mapping[str, Any] | None,
    run_providers: Iterable[str],
    item_providers: Mapping[str, str] | None = None,
) -> Verdict:
    """Check a batch of write rows. Refuse loudly, per row; never repair."""
    rules = Rules.from_ruleset(ruleset)
    providers = frozenset(run_providers)
    owners = item_providers or {}
    findings: list[Finding] = []

    targets: dict[str, str] = {}
    for row in rows:
        item_key = str(row.get("item_key") or "")
        findings.extend(check_row(row, rules, owners.get(item_key), providers))
        product_id = row.get("product_id")
        if isinstance(product_id, str) and row.get("action") in WRITING_ACTIONS:
            if product_id in targets:
                # Two items writing one product is a double decision. The unique index
                # on the stamp would refuse the second; saying so here names both.
                findings.append(Finding(
                    "target-shared", item_key or "<sin item_key>",
                    f"el mismo producto ya es destino de {targets[product_id]}"))
            else:
                targets[product_id] = item_key
    return Verdict(findings=tuple(findings))


#: The certification a caller can grep for. If this expression matches the comparison
#: engine's source, R-026 has been broken: a commission may never buy a worse price.
PRICE_TOLERANCE_SOURCE = (
    r"(?i)(?:price|commission)_?\w*tolerance|"
    r"(?:price|commission)_?\w*ratio\b|"
    r"\*\s*1\.[0-9]\d*|"
    r"\+\s*0?\.\d+\s*\*"
)


def price_tolerance_present(source: str) -> bool:
    """True when ``source`` contains a multiplicative price tolerance.

    A positive control exists for this in the tests: it returns ``True`` on the shape
    the legacy comparator had. A detector that has only ever passed proves nothing.
    """
    return bool(re.search(PRICE_TOLERANCE_SOURCE, source))


__all__ = [
    "WRITABLE_PRODUCT_KEYS",
    "WRITING_ACTIONS",
    "ROW_KEYS",
    "PRICE_TOLERANCE_SOURCE",
    "GateRefused",
    "Finding",
    "Verdict",
    "Rules",
    "check_row",
    "gate",
    "price_tolerance_present",
]
