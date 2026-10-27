"""Record linking and entity resolution (spec 6, 8.3).

The link between an external record and a master entity is managed
independently of value updates (spec 6.1): relinking is an operation of its
own and is recorded in history via the ``entity_links`` table (changes to
links are management data; attribute changes go through the master tables).
"""
import json

from .store import now


def match_candidates(base, entity_type, values):
    """Candidates for one source record, via the entity's match rule."""
    e = base.entities[entity_type]
    if e.match is None:
        return None
    return e.match(values, base)


def link(base, source_id, external_key, entity_type, entity_id, role=""):
    base.con.execute(
        "INSERT INTO entity_links (source_id, external_key, role, entity_type, entity_id,"
        " created_at) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(source_id, external_key, role) DO UPDATE SET"
        " entity_type=excluded.entity_type, entity_id=excluded.entity_id",
        (source_id, external_key, role, entity_type, entity_id, now()))
    base.con.commit()


def unlink(base, source_id, external_key, role=""):
    base.con.execute("DELETE FROM entity_links WHERE source_id=? AND external_key=? AND role=?",
                     (source_id, external_key, role))
    base.con.execute("DELETE FROM sync_states WHERE source_id=? AND external_key=? AND role=?",
                     (source_id, external_key, role))
    base.con.commit()


def _rows(base, sql, args):
    return [dict(r) for r in base.con.execute(sql, args)]


def detach(base, entity_type, entity_id, reason=""):
    """Detach everything that points at one master entity, with a record.

    Removes the entity's ``entity_links``, the ``sync_states`` baselines of
    those links and the ``relations`` from or to it, after saving them in
    ``detachments`` (kind ``detach``) under the current commit.  Use it
    before deleting a row that is no longer valid, so no live link points at
    a missing entity (``validate()``) while the past correspondence stays
    traceable.  ``revert``/``revert_change`` restore what was detached when
    they re-insert the row; ``revert`` of a commit that only detached
    restores it on its own (the commit is kept even without master changes).

    Must be called inside ``transaction()``; it never commits, so a failure
    later in the same transaction rolls the detachment back as well.
    Returns the detachment id, or None when nothing pointed at the entity.
    """
    cid = base.con.execute("SELECT commit_id FROM sync_context WHERE id=1").fetchone()[0]
    if cid is None:
        raise RuntimeError("detach must run inside transaction()")
    links = _rows(base, "SELECT * FROM entity_links WHERE entity_type=? AND entity_id=? ORDER BY id",
                  (entity_type, entity_id))
    states = []
    for l in links:
        states += _rows(base, "SELECT * FROM sync_states WHERE source_id=? AND external_key=? AND role=? ORDER BY id",
                        (l["source_id"], l["external_key"], l["role"]))
    rels = _rows(base, "SELECT * FROM relations WHERE (from_type=? AND from_id=?) OR (to_type=? AND to_id=?) ORDER BY id",
                 (entity_type, entity_id, entity_type, entity_id))
    if not (links or states or rels):
        return None
    did = base.con.execute(
        "INSERT INTO detachments (commit_id, ts, entity_type, entity_id, kind, reason, payload)"
        " VALUES (?,?,?,?,'detach',?,?)",
        (cid, now(), entity_type, entity_id, reason,
         json.dumps(dict(links=links, states=states, relations=rels), ensure_ascii=False))).lastrowid
    for s in states:
        base.con.execute("DELETE FROM sync_states WHERE id=?", (s["id"],))
    for l in links:
        base.con.execute("DELETE FROM entity_links WHERE id=?", (l["id"],))
    for r in rels:
        base.con.execute("DELETE FROM relations WHERE id=?", (r["id"],))
    return did


def _insert(base, table, row):
    cols = [c for c in row if c != "id"]
    base.con.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(cols), ", ".join("?" * len(cols))),
                     [row[c] for c in cols])


def restore(base, commit_id, entity_type, entity_id):
    """Re-attach what ``detach`` removed from one entity in ``commit_id``
    (used by revert, with a re-inserted row or for a detach-only entity).  All-or-nothing: raises
    RuntimeError, before writing anything, if a link key, a baseline or a
    relation slot is taken by something else now.  Records a ``restore``
    row per restored detachment.  Must run inside ``transaction()``."""
    cid = base.con.execute("SELECT commit_id FROM sync_context WHERE id=1").fetchone()[0]
    if cid is None:
        raise RuntimeError("restore must run inside transaction()")
    recs = _rows(base, "SELECT * FROM detachments WHERE commit_id=? AND entity_type=? AND entity_id=?"
                 " AND kind='detach' ORDER BY id", (commit_id, entity_type, entity_id))
    plans = [(d, json.loads(d["payload"])) for d in recs]
    taken = []
    for _, p in plans:
        for l in p["links"]:
            if base.con.execute("SELECT 1 FROM entity_links WHERE source_id=? AND external_key=? AND role=?",
                                (l["source_id"], l["external_key"], l["role"])).fetchone():
                taken.append("link %s/%s/%s" % (l["source_id"], l["external_key"], l["role"]))
        for s in p["states"]:
            if base.con.execute("SELECT 1 FROM sync_states WHERE source_id=? AND external_key=? AND role=? AND field=?",
                                (s["source_id"], s["external_key"], s["role"], s["field"])).fetchone():
                taken.append("baseline %s/%s/%s/%s" % (s["source_id"], s["external_key"], s["role"], s["field"]))
        for r in p["relations"]:
            if base.con.execute("SELECT 1 FROM relations WHERE kind=? AND from_type=? AND from_id=?",
                                (r["kind"], r["from_type"], r["from_id"])).fetchone():
                taken.append("relation %s %s/%s" % (r["kind"], r["from_type"], r["from_id"]))
    if taken:
        raise RuntimeError("cannot restore what was detached from %s/%s: now used by %s"
                           % (entity_type, entity_id, ", ".join(taken)))
    for d, p in plans:
        for l in p["links"]:
            _insert(base, "entity_links", l)
        for s in p["states"]:
            _insert(base, "sync_states", s)
        for r in p["relations"]:
            _insert(base, "relations", r)
        base.con.execute(
            "INSERT INTO detachments (commit_id, ts, entity_type, entity_id, kind, ref, reason, payload)"
            " VALUES (?,?,?,?,'restore',?,?,?)",
            (cid, now(), entity_type, entity_id, d["id"], d["reason"], d["payload"]))
    return len(plans)


def detachments(base, entity_type=None, entity_id=None, commit_id=None):
    """Detach/restore records, newest first (the history of correspondence
    that was taken off deleted entities)."""
    where, args = [], []
    for col, v in (("entity_type", entity_type), ("entity_id", entity_id), ("commit_id", commit_id)):
        if v is not None:
            where.append("%s=?" % col); args.append(v)
    out = _rows(base, "SELECT * FROM detachments" + (" WHERE " + " AND ".join(where) if where else "")
                + " ORDER BY id DESC", args)
    for d in out:
        d["payload"] = json.loads(d["payload"])
    return out


def links_of(base, entity_type, entity_id):
    return [dict(r) for r in base.con.execute(
        "SELECT * FROM entity_links WHERE entity_type=? AND entity_id=?",
        (entity_type, entity_id))]


def merge_entities(base, entity_type, keep, drop, note=""):
    """Merge entity ``drop`` into ``keep``: repoint links, states, decisions,
    then delete the dropped row (recorded as one commit)."""
    if keep == drop:
        raise ValueError("cannot merge an entity into itself")
    from .relations import merge_relations
    with base.transaction(note or ("merge %s %s into %s" % (entity_type, drop, keep)),
                          kind="merge"):
        for table in ("entity_links", "field_decisions"):
            base.con.execute(
                "UPDATE %s SET entity_id=? WHERE entity_type=? AND entity_id=?" % table,
                (keep, entity_type, drop))
        merge_relations(base, entity_type, keep, drop)
        base.delete(entity_type, drop)


def split_entity(base, entity_type, entity_id, link_ids, values=None, note=""):
    """Move the given link rows to a newly created entity (spec 6.1:
    undo a wrong merge).  ``values`` seeds the new row (defaults to a copy
    of the current one).  Returns the new entity id."""
    cur = base.values(entity_type, entity_id)
    if cur is None:
        raise KeyError("no such entity: %s/%s" % (entity_type, entity_id))
    with base.transaction(note or ("split %s %s" % (entity_type, entity_id)), kind="split"):
        new_id = base.insert(entity_type, values or cur)
        for lid in link_ids:
            base.con.execute(
                "UPDATE entity_links SET entity_id=? WHERE id=? AND entity_type=? AND entity_id=?",
                (new_id, lid, entity_type, entity_id))
    return new_id
