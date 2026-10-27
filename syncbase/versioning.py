"""Version history: inspect commits/changes, reconstruct, revert (spec 4.3).

Changes are recorded by the history triggers (store.py) in global order
(``changes.id``), whether they were made inside a commit scope or by direct
SQL.  A "version" is the state right after a commit's last change.
"""
import json

from .store import q


def _rows(base, sql, args=()):
    return [dict(r) for r in base.con.execute(sql, args)]


def history(base, entity_type=None, entity_id=None, limit=200):
    """Change history, newest first, joined with commit metadata (spec 10.3)."""
    where, args = [], []
    if entity_type:
        where.append("c.entity_type=?"); args.append(entity_type)
    if entity_id is not None:
        where.append("c.entity_id=?"); args.append(entity_id)
    return _rows(base,
        "SELECT c.id, c.commit_id, c.ts, c.entity_type, c.entity_id, c.op, c.old, c.new, "
        "k.actor, k.kind, k.batch_id, k.note FROM changes c LEFT JOIN commits k ON k.id=c.commit_id "
        + ("WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY c.id DESC LIMIT ?", args + [limit])


def commits(base, limit=200):
    return _rows(base, "SELECT * FROM commits ORDER BY id DESC LIMIT ?", (limit,))


def _boundary(base, commit_id):
    """The last change id that belongs to ``commit_id`` (0 = before history)."""
    if commit_id == 0:
        return 0
    r = base.con.execute("SELECT MAX(id) FROM changes WHERE commit_id=?", (commit_id,)).fetchone()
    if r[0] is None:
        if not base.con.execute("SELECT 1 FROM commits WHERE id=?", (commit_id,)).fetchone():
            raise KeyError("unknown commit: %s" % commit_id)
        # Commit exists but recorded no changes; use the previous change id.
        r = base.con.execute(
            "SELECT COALESCE(MAX(c.id),0) FROM changes c WHERE c.commit_id<=?", (commit_id,)).fetchone()
    return r[0] or 0


def get_version(base, commit_id):
    """Reconstruct ``{entity_type: {entity_id: row or None}}`` as of a commit.

    Only rows touched after the commit differ from the current state; the
    result contains every row of every registered entity for simplicity.
    """
    out = {}
    for name, e in base.entities.items():
        out[name] = {r[e.pk]: dict(r) for r in
                     (dict(x) for x in base.con.execute("SELECT * FROM %s" % q(e.table)))}
    b = _boundary(base, commit_id)
    later = _rows(base, "SELECT * FROM changes WHERE id>? ORDER BY id DESC", (b,))
    for c in later:
        ent = out.setdefault(c["entity_type"], {})
        old = json.loads(c["old"]) if c["old"] else None
        if old is None:
            ent[c["entity_id"]] = None       # the row did not exist yet
        else:
            ent[c["entity_id"]] = old
    for name in out:
        out[name] = {k: v for k, v in out[name].items() if v is not None}
    return out


def compare_versions(base, v1, v2):
    """Field-level differences between two commits (v1 older, v2 newer)."""
    if v1 > v2:
        v1, v2 = v2, v1
    b1, b2 = _boundary(base, v1), _boundary(base, v2)
    diffs = []
    for c in _rows(base, "SELECT * FROM changes WHERE id>? AND id<=? ORDER BY id", (b1, b2)):
        old = json.loads(c["old"]) if c["old"] else None
        new = json.loads(c["new"]) if c["new"] else None
        if c["op"] == "update":
            for f in new:
                if old.get(f) != new.get(f):
                    diffs.append(dict(entity_type=c["entity_type"], entity_id=c["entity_id"],
                                      op="update", field=f, old=old.get(f), new=new.get(f)))
        else:
            diffs.append(dict(entity_type=c["entity_type"], entity_id=c["entity_id"],
                              op=c["op"], field=None, old=old, new=new))
    return diffs


def _apply_inverse(base, c, force, label):
    """Apply the inverse of one change row (helper for revert/revert_change)."""
    e = base.entities[c["entity_type"]]
    old = json.loads(c["old"]) if c["old"] else None
    new = json.loads(c["new"]) if c["new"] else None
    cur = base.row(c["entity_type"], c["entity_id"])
    if c["op"] == "update":
        if cur is None:
            raise RuntimeError("row %s/%s no longer exists" % (c["entity_type"], c["entity_id"]))
        fields = [f for f in new if old.get(f) != new.get(f) and f != e.pk]
        stale = [f for f in fields if cur.get(f) != new.get(f)]
        if stale and not force:
            raise RuntimeError("values changed since %s: %s" % (label, ", ".join(stale)))
        base.update(c["entity_type"], c["entity_id"], {f: old.get(f) for f in fields})
    elif c["op"] == "insert":
        if cur is None:
            return
        if not force and any(cur.get(f) != new.get(f) for f in new if f != e.pk):
            raise RuntimeError("row %s/%s changed since %s"
                               % (c["entity_type"], c["entity_id"], label))
        base.delete(c["entity_type"], c["entity_id"], detach=True, reason=label)   # no dangling links
    elif c["op"] == "delete":
        if cur is not None and not force:
            raise RuntimeError("row %s/%s exists again" % (c["entity_type"], c["entity_id"]))
        if cur is None:
            cols = [k for k in old]
            base.con.execute(
                "INSERT INTO %s (%s) VALUES (%s)"
                % (q(e.table), ", ".join(q(k) for k in cols), ", ".join("?" * len(cols))),
                [old[k] for k in cols])
            from .links import restore
            if c["commit_id"] is not None:                # re-attach what was detached with the row
                restore(base, c["commit_id"], c["entity_type"], c["entity_id"])


def revert(base, commit_id, force=False, note=None):
    """Undo one commit by applying the inverse changes as a *new* commit
    (spec 4.3: never rewrite history).

    Refuses when a touched value has changed since, unless ``force``.
    Returns the new commit id.
    """
    rows = _rows(base, "SELECT * FROM changes WHERE commit_id=? ORDER BY id DESC", (commit_id,))
    if not rows:
        raise KeyError("commit %s has no changes" % commit_id)
    with base.transaction(note or ("revert commit %d" % commit_id), kind="revert") as cid:
        for c in rows:
            _apply_inverse(base, c, force, "commit %d" % commit_id)
        return cid


def revert_change(base, change_id, force=False, note=None):
    """Undo a single recorded change as a new commit (spec 4.3).

    Finer-grained than ``revert``: one row of ``changes`` instead of a whole
    commit.  Returns the new commit id.
    """
    c = base.con.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
    if not c:
        raise KeyError("unknown change: %s" % change_id)
    with base.transaction(note or ("revert change %d" % change_id), kind="revert") as cid:
        _apply_inverse(base, dict(c), force, "change %d" % change_id)
        return cid
