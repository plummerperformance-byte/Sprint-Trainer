"""Export a PPA session in a 1080-compatible format.

    python -m ppa.cli.export --session-id 42 --format json --strict --out s42.json
    python -m ppa.cli.export --session-id 42 --format csv-summary --out s42.csv
    python -m ppa.cli.export --session-id 42 --format csv-samples

Formats: ``json`` (1080-shaped session document), ``csv-summary`` (one row per
rep), ``csv-samples`` (one row per sample). ``--strict`` drops every PPA-only
field/column — pure 1080 schema, a drop-in replacement; without it the export
is the richer PPA superset. If ``--out`` is omitted a default filename is
generated: ``{session}_{client}_{date}_{format}.{ext}``.

Australian English throughout.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys

import persistence

from ppa.export.csv_1080 import export_session_csv_samples, export_session_csv_summary
from ppa.export.json_1080 import build_session_doc

_FORMATS = ("json", "csv-summary", "csv-samples")
_EXT = {"json": "json", "csv-summary": "csv", "csv-samples": "csv"}


def _default_filename(conn: sqlite3.Connection, session_id: int, fmt: str) -> str:
    """{session}_{client}_{date}_{format}.{ext}, with the client name slugged."""
    row = conn.execute(
        "SELECT a.name AS name, s.started_at AS started "
        "FROM sessions s JOIN athletes a ON a.id = s.athlete_id "
        "WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    client = "unknown"
    date = "nodate"
    if row is not None:
        client = re.sub(r"[^A-Za-z0-9]+", "-", (row["name"] or "unknown")).strip("-")
        date = (row["started"] or "nodate").split("T")[0].split(" ")[0]
    return f"{session_id}_{client}_{date}_{fmt}.{_EXT[fmt]}"


def render(conn: sqlite3.Connection, session_id: int, fmt: str,
           strict: bool) -> str:
    if fmt == "json":
        import json as _json
        return _json.dumps(
            build_session_doc(conn, session_id, strict=strict), indent=2)
    if fmt == "csv-summary":
        return export_session_csv_summary(conn, session_id, strict=strict)
    if fmt == "csv-samples":
        return export_session_csv_samples(conn, session_id, strict=strict)
    raise ValueError(f"unknown format {fmt}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ppa.cli.export",
        description="Export a PPA session in a 1080-compatible format.",
    )
    ap.add_argument("--session-id", type=int, required=True)
    ap.add_argument("--format", default="json", choices=_FORMATS)
    ap.add_argument("--strict", action="store_true",
                    help="drop all PPA-only fields — pure 1080 schema")
    ap.add_argument("--out", help="output file (default: an auto-generated name)")
    ap.add_argument("--db", default=persistence.DB_PATH,
                    help="path to the PPA SQLite database")
    args = ap.parse_args(argv)

    conn = persistence.open_db(args.db)
    try:
        text = render(conn, args.session_id, args.format, args.strict)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    out = args.out or _default_filename(conn, args.session_id, args.format)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
