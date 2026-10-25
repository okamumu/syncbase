"""Data provenance (spec 3.3): where each adopted value came from.

Adoptions are recorded in ``field_decisions`` by ``staging.commit`` and
``staging.resolve`` (``manual`` distinguishes automatic adoption from a human
decision; ``note`` carries the judgement).  This module provides the query
side.
"""
import json


def provenance(base, entity_type, entity_id, field=None):
    """Adoption history of an entity's fields, oldest first."""
    where = "entity_type=? AND entity_id=?"
    args = [entity_type, entity_id]
    if field is not None:
        where += " AND field=?"
        args.append(field)
    out = []
    for r in base.con.execute(
            "SELECT * FROM field_decisions WHERE %s ORDER BY id" % where, args):
        out.append(dict(field=r["field"],
                        value=json.loads(r["value"]) if r["value"] is not None else None,
                        manual=bool(r["manual"]), source_id=r["source_id"],
                        note=r["note"], decided_at=r["decided_at"]))
    return out


def decide(base, entity_type, entity_id, field, value, note=""):
    """Record a manual confirmation of a master value (spec 5.5).

    Under the ``protected`` policy the confirmed value is never overwritten
    silently by a source; a differing source value becomes a conflict.
    """
    from .staging import _decide
    _decide(base, entity_type, entity_id, field, value, True, None, note)
    base.con.commit()
