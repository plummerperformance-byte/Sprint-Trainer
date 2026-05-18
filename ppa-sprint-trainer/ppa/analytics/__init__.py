"""PPA-specific analytics — export-only derivations.

Kept separate from the top-level ``analytics.py`` (the live-rig module) so
export maths never pollutes the rig code path.
"""
from ppa.analytics import load_advisor
from ppa.analytics.load_advisor import suggest_load

__all__ = ["load_advisor", "suggest_load"]
