"""Collect a poe.ninja price snapshot into `price_history.db`.

`poe_market` only reads; this is the writer. Run it manually or on a schedule
(Task Scheduler / cron) and the risers, fallers and history tools start having
something to say.

Usage
-----
    python trend_watcher.py --league Covens
    python trend_watcher.py --league Covens --types Currency,Scarab,Fragment
    python trend_watcher.py --league Standard --no-stash --delay 3

Notes
-----
* Requests are serialized with a delay between them (default 1.5 s) and carry an
  identifying User-Agent — see legal_considerations.md. This is a third-party
  site, not GGG, but the same courtesy applies.
* Item names are the key. For gems, level/quality/corruption are appended so
  different variants do not overwrite each other; uniques keep the plain name,
  where the last variant in a snapshot wins.
* A failed type is skipped, not fatal — poe.ninja renames categories now and
  then, and one bad name should not lose the whole snapshot.

Version: 1.0
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Type lists live in poe_pricer so the watcher and the live pricer always cover
# the same categories. Fall back to a local copy if that import ever breaks.
try:
    from poe_pricer import _EXCHANGE_TYPES, _STASH_TYPES
except Exception:  # pragma: no cover - defensive
    _EXCHANGE_TYPES = [
        "Currency", "Fragment", "DivinationCard", "Scarab", "Essence", "Oil",
        "Fossil", "Omen", "Tattoo", "AllflameEmber", "Artifact", "DeliriumOrb",
        "Astrolabe", "Resonator", "Wombgift", "Incubator",
    ]
    _STASH_TYPES = [
        "UniqueWeapon", "UniqueArmour", "UniqueAccessory", "UniqueFlask", "UniqueJewel",
        "ForbiddenJewel", "ShrineBelt", "UniqueTincture", "UniqueRelic",
        "SkillGem", "ClusterJewel",
        "Map", "BlightedMap", "BlightRavagedMap", "UniqueMap",
        "Invitation", "BaseType", "Beast", "Vial",
    ]

EXCHANGE_URL = "https://poe.ninja/poe1/api/economy/exchange/current/overview"
STASH_URL = "https://poe.ninja/poe1/api/economy/stash/current/item/overview"

DEFAULT_DELAY = 1.5


def user_agent() -> str:
    """Identify honestly; never claim to be a browser."""
    ua = "poe-trade-mcp/1.0"
    contact = os.environ.get("POE_CONTACT_EMAIL", "").strip()
    if contact:
        ua += f" (contact: {contact})"
    return ua


def _header_safe(value: str) -> str:
    return "".join(
        ch if 32 <= ord(ch) < 127 else urllib.parse.quote(ch, safe="")
        for ch in value
    )


def _get(url: str, league: str, type_name: str) -> dict:
    """Fetch one poe.ninja category. Raises on failure."""
    full = f"{url}?league={urllib.parse.quote(league)}&type={urllib.parse.quote(type_name)}"
    req = urllib.request.Request(full, headers={
        "User-Agent": _header_safe(user_agent()),
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _gem_suffix(line: dict) -> str:
    parts = []
    if line.get("gemLevel"):
        parts.append(f"lvl {line['gemLevel']}")
    if line.get("gemQuality"):
        parts.append(f"{line['gemQuality']}%")
    if line.get("links"):
        parts.append(f"{line['links']}L")
    if line.get("corrupted"):
        parts.append("corrupted")
    return (" " + " ".join(parts)) if parts else ""


def collect(league: str,
            exchange_types: list[str] | None = None,
            stash_types: list[str] | None = None,
            delay: float = DEFAULT_DELAY,
            quiet: bool = False) -> tuple[list[dict], list[str]]:
    """Fetch every category and return (rows, failed_types)."""
    rows: list[dict] = []
    failed: list[str] = []
    exchange_types = _EXCHANGE_TYPES if exchange_types is None else exchange_types
    stash_types = _STASH_TYPES if stash_types is None else stash_types

    def log(msg: str) -> None:
        if not quiet:
            print(msg, file=sys.stderr)

    for type_name in exchange_types:
        try:
            data = _get(EXCHANGE_URL, league, type_name)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as e:
            failed.append(type_name)
            log(f"  {type_name:<20} FAILED ({type(e).__name__})")
            time.sleep(delay)
            continue

        names = {i.get("id"): i.get("name") for i in data.get("items", [])}
        count = 0
        for line in data.get("lines", []):
            name = names.get(line.get("id"))
            value = line.get("primaryValue")
            if not name or not value:
                continue
            rows.append({
                "name": name,
                "category": type_name,
                "chaos_value": round(float(value), 4),
                "listing_count": int(line.get("volumePrimaryValue") or 0),
            })
            count += 1
        log(f"  {type_name:<20} {count} items")
        time.sleep(delay)

    for type_name in stash_types:
        try:
            data = _get(STASH_URL, league, type_name)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as e:
            failed.append(type_name)
            log(f"  {type_name:<20} FAILED ({type(e).__name__})")
            time.sleep(delay)
            continue

        count = 0
        for line in data.get("lines", []):
            name = line.get("name")
            value = line.get("chaosValue")
            if not name or not value:
                continue
            qualifier = _gem_suffix(line) if type_name in ("SkillGem", "ClusterJewel") else ""
            rows.append({
                "name": name + qualifier,
                "category": type_name,
                "chaos_value": round(float(value), 4),
                "divine_value": round(float(line["divineValue"]), 4)
                                if line.get("divineValue") else None,
                "listing_count": line.get("listingCount"),
            })
            count += 1
        log(f"  {type_name:<20} {count} items")
        time.sleep(delay)

    return rows, failed


def snapshot(league: str, **kwargs) -> dict:
    """Collect one snapshot and persist it. Returns the summary dict."""
    import price_db

    rows, failed = collect(league, **kwargs)
    if not rows:
        raise RuntimeError(
            "poe.ninja returned nothing — check the league name "
            f"({league!r}) and your connection."
        )
    summary = price_db.record_snapshot(rows, source=f"poe.ninja/{league}")
    summary["failed_types"] = failed
    summary["league"] = league
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snapshot poe.ninja prices into price_history.db")
    parser.add_argument("--league", default=os.environ.get("POE_LEAGUE", ""),
                        help="League name (default: $POE_LEAGUE)")
    parser.add_argument("--db", default="",
                        help="Path to price_history.db (default: alongside this module)")
    parser.add_argument("--types", default="",
                        help="Comma-separated category override, e.g. Currency,Scarab")
    parser.add_argument("--exchange-only", action="store_true", help="Skip stash-endpoint types")
    parser.add_argument("--stash-only", action="store_true", help="Skip exchange-endpoint types")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"Seconds between requests (default {DEFAULT_DELAY})")
    parser.add_argument("--quiet", action="store_true", help="Only print the JSON summary")
    args = parser.parse_args(argv)

    if not args.league:
        parser.error("--league is required (or set POE_LEAGUE)")
    if args.db:
        os.environ["POE_PRICE_DB"] = args.db

    exchange_types = None if args.stash_only else (_EXCHANGE_TYPES if not args.types else None)
    stash_types = None if args.exchange_only else (_STASH_TYPES if not args.types else None)
    if args.types:
        chosen = [t.strip() for t in args.types.split(",") if t.strip()]
        exchange_types = [] if args.stash_only else chosen
        stash_types = [] if args.exchange_only else chosen

    try:
        summary = snapshot(
            args.league,
            exchange_types=exchange_types,
            stash_types=stash_types,
            delay=max(0.0, args.delay),
            quiet=args.quiet,
        )
    except Exception as e:
        print(f"trend_watcher: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
