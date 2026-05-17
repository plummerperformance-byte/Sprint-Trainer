"""1080-shaped JSON export of a PPA session.

Builds the session as the proto **wire-format** models
(``ppa.models.onezeroeightzero`` / ``ppa.models.ppa``) — a
``GetDataForSession``-shaped ``{"session": …, "sets": […]}`` document. With
``strict=True`` every ``ppa_*`` field is dropped, so the output validates as
pure 1080 schema.

Australian English throughout.
"""
from __future__ import annotations

import json
import sqlite3

import persistence

from ppa.adapters.sqlite_to_ppa import load_session, load_session_sets


def _strip_ppa(obj):
    """Recursively drop every ``ppa_``-prefixed key."""
    if isinstance(obj, dict):
        return {k: _strip_ppa(v) for k, v in obj.items() if not k.startswith("ppa_")}
    if isinstance(obj, list):
        return [_strip_ppa(x) for x in obj]
    return obj


def build_session_doc(conn: sqlite3.Connection, session_id: int,
                      strict: bool = False) -> dict:
    """Build the 1080-shaped document for a session as a JSON-able dict."""
    session = load_session(conn, session_id)
    sets = load_session_sets(conn, session_id)
    doc = {
        "session": session.model_dump(mode="json", by_alias=True),
        "sets": [s.model_dump(mode="json", by_alias=True) for s in sets],
    }
    return _strip_ppa(doc) if strict else doc


def export_session_json(session_id: int, strict: bool = False,
                        db_path: str | None = None) -> str:
    """Export a PPA session as 1080-shaped JSON text.

    Args:
        session_id: the PPA session to export.
        strict: drop all ``ppa_*`` fields — pure 1080 schema.
        db_path: path to the PPA database (defaults to ``persistence.DB_PATH``).
    """
    conn = persistence.open_db(db_path or persistence.DB_PATH)
    return json.dumps(build_session_doc(conn, session_id, strict=strict), indent=2)
