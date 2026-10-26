"""Master-to-master record linking with judgement history (spec 6.1, 6.4).

``entity_links`` maps *source records* to master entities.  Entity
resolution additionally needs relations between master records themselves
(e.g. an author mention row → the person it denotes), and a record of the
human judgements behind them: which candidates were declared the same,
different, or deferred, why, and what was compared.  Recording "different"
keeps settled pairs from being proposed again (spec 6.4).

A relation is a single *resolution pointer*: one per ``(kind, from)``
(``UNIQUE`` constrained), not a general many-to-many edge.  Model
many-to-many associations in your own master tables; this table answers
"what does this record resolve to".

Verdicts: ``same`` / ``different`` / ``deferred`` / ``reopen`` (clears the
pair's current state).  The latest judgement per unordered pair wins;
judgements themselves are append-only history.
"""
from .store import now

VERDICTS = ("same", "different", "deferred", "reopen")


def relate(base, kind, from_ref, to_ref, reason="", actor=None, commit=True):
    """Set (or repoint) the relation of one record.  ``from_ref``/``to_ref``
    are ``(entity_type, entity_id)``.  One relation per (kind, from).
    ``commit=False`` leaves committing to the caller's transaction."""
    base.con.execute(
        "INSERT INTO relations (kind, from_type, from_id, to_type, to_id, reason, actor, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(kind, from_type, from_id) DO UPDATE SET"
        " to_type=excluded.to_type, to_id=excluded.to_id, reason=excluded.reason,"
        " actor=excluded.actor, created_at=excluded.created_at",
        (kind, from_ref[0], from_ref[1], to_ref[0], to_ref[1], reason, actor or base.actor, now()))
    if commit:
        base.con.commit()


def unrelate(base, kind, from_ref):
    base.con.execute("DELETE FROM relations WHERE kind=? AND from_type=? AND from_id=?",
                     (kind, from_ref[0], from_ref[1]))
    base.con.commit()


def relation_of(base, kind, from_ref):
    r = base.con.execute("SELECT * FROM relations WHERE kind=? AND from_type=? AND from_id=?",
                         (kind, from_ref[0], from_ref[1])).fetchone()
    return dict(r) if r else None


def relations(base, kind, to_ref=None):
    """All relations of a kind, optionally only those pointing at ``to_ref``."""
    if to_ref is None:
        rows = base.con.execute("SELECT * FROM relations WHERE kind=? ORDER BY id", (kind,))
    else:
        rows = base.con.execute("SELECT * FROM relations WHERE kind=? AND to_type=? AND to_id=?"
                                " ORDER BY id", (kind, to_ref[0], to_ref[1]))
    return [dict(r) for r in rows]


def judge(base, kind, a_ref, b_ref, verdict, reason="", compared=None, actor=None, commit=True):
    """Record a resolution judgement about the pair (a, b) (spec 5.4/6.4).

    ``compared`` is any JSON string describing what was looked at.  The pair
    is unordered; it is stored in a canonical order.
    """
    if verdict not in VERDICTS:
        raise ValueError("unknown verdict: %r" % (verdict,))
    a, b = sorted([tuple(a_ref), tuple(b_ref)])
    base.con.execute(
        "INSERT INTO judgements (kind, a_type, a_id, b_type, b_id, verdict, reason, compared, actor, ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (kind, a[0], a[1], b[0], b[1], verdict, reason, compared, actor or base.actor, now()))
    if commit:
        base.con.commit()


def judgements(base, kind, ref=None, limit=500):
    """Judgement history, newest first; ``ref`` filters to pairs involving it."""
    if ref is None:
        rows = base.con.execute("SELECT * FROM judgements WHERE kind=? ORDER BY id DESC LIMIT ?",
                                (kind, limit))
    else:
        rows = base.con.execute(
            "SELECT * FROM judgements WHERE kind=? AND ((a_type=? AND a_id=?) OR (b_type=? AND b_id=?))"
            " ORDER BY id DESC LIMIT ?", (kind, ref[0], ref[1], ref[0], ref[1], limit))
    return [dict(r) for r in rows]


def pair_verdicts(base, kind):
    """Current state per pair: the latest verdict, with ``reopen`` clearing it.
    Returns ``{frozenset({a_ref, b_ref}): (verdict, reason)}``."""
    out = {}
    for r in base.con.execute("SELECT * FROM judgements WHERE kind=? ORDER BY id", (kind,)):
        pair = frozenset(((r["a_type"], r["a_id"]), (r["b_type"], r["b_id"])))
        if r["verdict"] == "reopen":
            out.pop(pair, None)
        else:
            out[pair] = (r["verdict"], r["reason"])
    return out


def merge_relations(base, entity_type, keep, drop):
    """Repoint relations when two master records merge (used by
    ``merge_entities``; call directly when merging outside syncbase).

    Relations pointing *at* the dropped record are repointed to ``keep``.
    Relations *from* the dropped record are repointed too, except when
    ``keep`` already has one of the same kind — then the keep side wins and
    the dropped record's relation is removed.  A relation that would end up
    pointing at its own record is dropped."""
    base.con.execute("UPDATE relations SET to_id=? WHERE to_type=? AND to_id=?",
                     (keep, entity_type, drop))
    base.con.execute("UPDATE OR IGNORE relations SET from_id=? WHERE from_type=? AND from_id=?",
                     (keep, entity_type, drop))
    base.con.execute("DELETE FROM relations WHERE from_type=? AND from_id=?", (entity_type, drop))
    base.con.execute("DELETE FROM relations WHERE from_type=to_type AND from_id=to_id")
