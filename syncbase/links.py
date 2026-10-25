"""Record linking and entity resolution (spec 6, 8.3).

The link between an external record and a master entity is managed
independently of value updates (spec 6.1): relinking is an operation of its
own and is recorded in history via the ``entity_links`` table (changes to
links are management data; attribute changes go through the master tables).
"""
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


def links_of(base, entity_type, entity_id):
    return [dict(r) for r in base.con.execute(
        "SELECT * FROM entity_links WHERE entity_type=? AND entity_id=?",
        (entity_type, entity_id))]


def merge_entities(base, entity_type, keep, drop, note=""):
    """Merge entity ``drop`` into ``keep``: repoint links, states, decisions,
    then delete the dropped row (recorded as one commit)."""
    if keep == drop:
        raise ValueError("cannot merge an entity into itself")
    with base.transaction(note or ("merge %s %s into %s" % (entity_type, drop, keep)),
                          kind="merge"):
        for table in ("entity_links", "field_decisions"):
            base.con.execute(
                "UPDATE %s SET entity_id=? WHERE entity_type=? AND entity_id=?" % table,
                (keep, entity_type, drop))
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
