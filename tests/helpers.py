"""Synthetic fixtures. Nothing here is a real product, supplier or price."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

A, B = "PRV_AAAAAAAA", "PRV_BBBBBBBB"
NOW = 1_800_000_000.0  # a fixed "snapshot instant" so staleness is deterministic


def iso(hours_ago: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW - hours_ago * 3600, timezone.utc).isoformat()


def offer(key, name, price, provider=A, lines=(), commission=None):
    return {
        "item_key": key, "provider_key": provider, "ordinal": int(key.rsplit("-", 1)[1]),
        "name": name, "price": price,
        "prices": [] if price is None else [{"value": price, "context": "", "scope": "generic",
                                             "source_provider": provider}],
        "commission": None if commission is None else {**commission, "source_provider": provider},
        "spec_lines": [{"label": "", "scope": "generic", "source_provider": provider, "text": t}
                       for t in lines],
        "text": "\n".join(lines), "observed_at": iso(1),
    }


def product(pid, name, price, provider=A, hours_ago=1.0):
    stamp = iso(hours_ago)
    return {"id": pid, "name": name, "base_price": price, "provider_public_key": provider,
            "created_at": iso(hours_ago + 100), "updated_at": stamp}


def uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


RULESET = {
    "enums": {"icons": ["truck", "shield", "money", "doc"], "commission_types": ["fixed", "percentage"]},
    "limits": {
        "conditions[].title": {"min_len": 1, "max_len": 100},
        "conditions[].details": {"max_len": 500},
        "commission.notes": {"max_len": 500},
        "commission.value": {"minimum": 0},
    },
}
