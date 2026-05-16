"""Export a PPA session as 1080-shaped JSON.

    python -m ppa.cli.export --session-id 42 --format 1080-json --out s42.json

The document mirrors a 1080 ``GetDataForSession`` response — the ``Session``
header plus its ``Set -> MotionGroup -> Motion`` tree. With ``--strict`` every
``ppa_``-prefixed field is dropped, so the output is pure 1080 schema (the
"pretend we're 1080" mode used to prove compatibility).

Australian English used throughout.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

import persistence

from ppa.adapters.sqlite_to_ppa import load_session, load_session_sets


def _strip_ppa(obj):
    """Recursively drop every ``ppa_``-prefixed key from a JSON-able tree."""
    if isinstance(obj, dict):
        return {k: _strip_ppa(v) for k, v in obj.items() if not k.startswith("ppa_")}
    if isinstance(obj, list):
        return [_strip_ppa(x) for x in obj]
    return obj


def export_session(conn: sqlite3.Connection, session_id: int,
                    strict: bool = False) -> dict:
    """Build the 1080-shaped JSON document for a session.

    Args:
        conn: open connection to ``ppa.db``.
        session_id: the PPA session to export.
        strict: if True, drop all ``ppa_*`` fields (pure 1080 schema).

    Returns:
        A JSON-able dict ``{"session": {...}, "sets": [...]}``.
    """
    session = load_session(conn, session_id)
    sets = load_session_sets(conn, session_id)
    # by_alias=True so verbatim proto wire names are used (e.g. the
    # MotionGroup ``_is_curve_selected`` field).
    doc = {
        "session": session.model_dump(mode="json", by_alias=True),
        "sets": [s.model_dump(mode="json", by_alias=True) for s in sets],
    }
    return _strip_ppa(doc) if strict else doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ppa.cli.export",
        description="Export a PPA session as 1080-shaped JSON.",
    )
    ap.add_argument("--session-id", type=int, required=True,
                    help="PPA session id to export")
    ap.add_argument("--format", default="1080-json", choices=["1080-json"],
                    help="output format (only 1080-json is supported)")
    ap.add_argument("--out", help="output file (defaults to stdout)")
    ap.add_argument("--strict", action="store_true",
                    help="drop all ppa_* fields — emit pure 1080 schema")
    ap.add_argument("--db", default=persistence.DB_PATH,
                    help="path to the PPA SQLite database")
    args = ap.parse_args(argv)

    conn = persistence.open_db(args.db)
    try:
        doc = export_session(conn, args.session_id, strict=args.strict)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    text = json.dumps(doc, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.out} ({len(doc['sets'])} set(s))")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
