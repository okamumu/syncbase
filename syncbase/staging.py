"""Staging: stage / diff / conflicts / resolve / commit (spec 8.1, 4.4, 11).

A *batch* is one observation of a source: ``stage()`` stores the raw records
(source data is observation history and is never overwritten, spec 3.1),
``diff()`` classifies every provided field with the three-way merge,
``resolve()`` records human decisions for conflicts, and ``commit()`` applies
everything in one transaction and advances the synchronization state.

The mapping passed to ``stage()`` routes payload fields onto linked
entities:  ``{payload_field: (entity_type, role, column)}``.  ``role``
distinguishes several entities linked to the same external record (e.g. a
form response that yields one talk and several authors); adapters flatten
repeating groups into roles before staging.
"""
import json

from . import merge
from .store import now


class StaleView(RuntimeError):
    """The master changed between preview and commit (spec 11.2)."""


class Unresolved(RuntimeError):
    """Conflicts without a decision block the commit (spec 5.4)."""

    def __init__(self, items):
        self.items = items
        super().__init__("%d unresolved conflict(s); resolve them first" % len(items))


def _j(v):
    return json.dumps(v, ensure_ascii=False)


def _load(v):
    return json.loads(v) if v is not None else None


def stage(base, source_id, records, mapping):
    """Store one observed batch.  ``records`` is ``{external_key: payload}``.

    Inside an open ``transaction()`` it joins that transaction (no commit of
    its own) and records the latest confirmed commit as ``base_commit``."""
    base.get_source(source_id)
    for f, spec in mapping.items():
        if len(spec) != 3:
            raise ValueError("mapping for %r must be (entity_type, role, column)" % f)
        if spec[0] not in base.entities:
            raise KeyError("unknown entity type in mapping: %s" % spec[0])
    cur = base.con.execute(
        "INSERT INTO batches (source_id, mapping, created_at, base_commit) VALUES (?,?,?,?)",
        (source_id, _j({k: list(v) for k, v in mapping.items()}), now(), base.head()))
    batch_id = cur.lastrowid
    for key, payload in records.items():
        base.con.execute(
            "INSERT INTO source_records (source_id, batch_id, external_key, payload, observed_at) "
            "VALUES (?,?,?,?,?)", (source_id, batch_id, str(key), _j(payload), now()))
    base._autocommit()
    return batch_id


def _batch(base, batch_id):
    b = base.con.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not b:
        raise KeyError("unknown batch: %s" % batch_id)
    mapping = {k: tuple(v) for k, v in json.loads(b["mapping"]).items()}
    records = {r["external_key"]: json.loads(r["payload"]) for r in base.con.execute(
        "SELECT * FROM source_records WHERE batch_id=? ORDER BY id", (batch_id,))}
    return b, mapping, records


def _links(base, source_id):
    out = {}
    for r in base.con.execute("SELECT * FROM entity_links WHERE source_id=?", (source_id,)):
        out[(r["external_key"], r["role"])] = (r["entity_type"], r["entity_id"])
    return out


def _states(base, source_id, external_key):
    out = {}
    for r in base.con.execute(
            "SELECT * FROM sync_states WHERE source_id=? AND external_key=?",
            (source_id, external_key)):
        out[(r["role"], r["field"])] = (_load(r["s0"]), _load(r["m0"]))
    return out


def _decided(base, entity_type, entity_id):
    out = {}
    for r in base.con.execute(
            "SELECT field, value, manual FROM field_decisions WHERE entity_type=? AND entity_id=? "
            "ORDER BY id", (entity_type, entity_id)):
        out[r["field"]] = _load(r["value"]) if r["manual"] else merge.MISSING
    return {f: v for f, v in out.items() if v is not merge.MISSING}


def _by_role(mapping, payload):
    """Split a payload into ``{(entity_type, role): {column: value}}``."""
    out = {}
    for f, (etype, role, column) in mapping.items():
        if f in payload:
            out.setdefault((etype, role), {})[column] = payload[f]
    return out


def diff(base, batch_id):
    """Classify the batch against the current master.  Read-only (spec 10.1).

    Returns a dict with ``head`` (database version at diff time, for the
    optimistic check in ``commit``), ``records`` and ``vanished``.
    """
    b, mapping, records = _batch(base, batch_id)
    source = base.get_source(b["source_id"])
    links = _links(base, b["source_id"])
    out = dict(batch=batch_id, source=b["source_id"], head=base.head(),
               records=[], vanished=[])
    for key, payload in records.items():
        states = _states(base, b["source_id"], key)
        rec = dict(external_key=key, roles=[])
        for (etype, role), cols in _by_role(mapping, payload).items():
            link = links.get((key, role))
            if link is None:
                e = base.entities[etype]
                cand = e.match(cols, base) if e.match else None
                rec["roles"].append(dict(entity_type=etype, role=role, status="new",
                                         values=cols, candidate=cand))
                continue
            m1 = base.values(link[0], link[1])
            if m1 is None:
                rec["roles"].append(dict(entity_type=link[0], role=role, status="deleted_master",
                                         entity_id=link[1], values=cols))
                continue
            s0 = {f: s for (r0, f), (s, _) in states.items() if r0 == role}
            m0 = {f: m for (r0, f), (_, m) in states.items() if r0 == role}
            cls = merge.three_way(s0, cols, m0, m1,
                                  base.entities[link[0]].policies,
                                  _decided(base, link[0], link[1]))
            fields = {f: dict(status=st, apply=ap, value=val,
                              s0=s0.get(f, None) if f in s0 else None,
                              s1=cols.get(f), m0=m0.get(f), m1=m1.get(f))
                      for f, (st, ap, val) in cls.items()}
            sts = {x["status"] for x in fields.values()}
            unresolved = any(x["status"] == merge.CONFLICT and not x["apply"] for x in fields.values())
            status = ("conflict" if unresolved
                      else "update" if sts & {merge.SOURCE_UPDATE, merge.AGREED,
                                              merge.MASTER_KEPT, merge.CONFLICT}
                      else "same")
            rec["roles"].append(dict(entity_type=link[0], role=role, status=status,
                                     entity_id=link[1], fields=fields))
        out["records"].append(rec)
    if source.kind == "full":
        seen = set(records)
        done = [r["external_key"] for r in base.con.execute(
            "SELECT DISTINCT external_key FROM source_records WHERE source_id=? "
            "AND accepted_at IS NOT NULL", (b["source_id"],))]
        for key in done:
            if key in seen:
                continue
            linked = [(role, et, eid) for (k, role), (et, eid) in links.items() if k == key]
            if linked:                      # rows added directly to the master have no
                out["vanished"].append(     # link and are never touched (spec 5.6)
                    dict(external_key=key, links=linked))
    return out


def conflicts(base, batch_id):
    """Pending conflicts of a batch, with any stored resolutions (spec 10.2)."""
    d = diff(base, batch_id)
    stored = {(r["external_key"], r["role"], r["field"]): dict(r) for r in base.con.execute(
        "SELECT * FROM conflicts WHERE batch_id=?", (batch_id,))}
    out = []
    for rec in d["records"]:
        for role in rec["roles"]:
            for f, x in (role.get("fields") or {}).items():
                if x["status"] != merge.CONFLICT:
                    continue
                s = stored.get((rec["external_key"], role["role"], f))
                out.append(dict(external_key=rec["external_key"], role=role["role"],
                                entity_type=role["entity_type"], entity_id=role["entity_id"],
                                field=f, s0=x["s0"], s1=x["s1"], m0=x["m0"], m1=x["m1"],
                                auto=x["apply"],
                                resolution=s and s["resolution"],
                                value=_load(s["value"]) if s and s["value"] is not None else None))
    return out


def resolve(base, batch_id, decisions, note=""):
    """Record conflict decisions (spec 5.4).  ``decisions`` maps
    ``(external_key, role, field)`` (or ``(external_key, field)`` for the
    default role) to ``"source"`` | ``"master"`` | ``("value", v)`` |
    ``"defer"``.  Decisions are stored with the batch and in history."""
    cs = {(c["external_key"], c["role"], c["field"]): c for c in conflicts(base, batch_id)}
    for k, res in decisions.items():
        key = (k[0], "", k[1]) if len(k) == 2 else tuple(k)
        if key not in cs:
            raise KeyError("no such conflict: %r" % (key,))
        value = None
        if isinstance(res, (tuple, list)):
            if res[0] != "value":
                raise ValueError("bad resolution: %r" % (res,))
            res, value = "value", _j(res[1])
        elif res not in ("source", "master", "defer"):
            raise ValueError("bad resolution: %r" % (res,))
        c = cs[key]
        base.con.execute(
            "INSERT INTO conflicts (batch_id, source_id, external_key, role, field, entity_type,"
            " entity_id, s0, s1, m0, m1, resolution, value, note, resolved_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(batch_id, external_key, role, field) DO UPDATE SET"
            " resolution=excluded.resolution, value=excluded.value, note=excluded.note,"
            " resolved_at=excluded.resolved_at",
            (batch_id, diff_source(base, batch_id), key[0], key[1], key[2],
             c["entity_type"], c["entity_id"], _j(c["s0"]), _j(c["s1"]), _j(c["m0"]), _j(c["m1"]),
             res, value, note, now()))
    base.con.commit()


def diff_source(base, batch_id):
    b = base.con.execute("SELECT source_id FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not b:
        raise KeyError("unknown batch: %s" % batch_id)
    return b["source_id"]


def _set_state(base, source_id, key, role, field, s1, m_now):
    base.con.execute(
        "INSERT INTO sync_states (source_id, external_key, role, field, s0, m0, updated_at)"
        " VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(source_id, external_key, role, field) DO UPDATE SET"
        " s0=excluded.s0, m0=excluded.m0, updated_at=excluded.updated_at",
        (source_id, key, role, field, _j(s1), _j(m_now), now()))


def _observe(base, source_id, key, role, field, s1, m_now):
    """Advance only the source side (S0) of the baseline; keep the master
    side (M0) where it was.  Used for MASTER_KEPT fields: re-observing an
    unchanged source does not reconcile a master-only edit (spec 4.4,
    5.5), so a later source change still meets it as a conflict.  Without
    a stored baseline (first contact) both sides are recorded."""
    cur = base.con.execute(
        "UPDATE sync_states SET s0=?, updated_at=? WHERE source_id=? AND external_key=?"
        " AND role=? AND field=?", (_j(s1), now(), source_id, key, role, field))
    if cur.rowcount == 0:
        _set_state(base, source_id, key, role, field, s1, m_now)


def _decide(base, etype, eid, field, value, manual, source_id, note=""):
    base.con.execute(
        "INSERT INTO field_decisions (entity_type, entity_id, field, value, manual, source_id,"
        " note, decided_at) VALUES (?,?,?,?,?,?,?,?)",
        (etype, eid, field, _j(value), 1 if manual else 0, source_id, note, now()))


def commit(base, batch_id, expected_head=None, note=""):
    """Apply a batch in one transaction (spec 11.1) and advance sync state.

    The diff is recomputed; ``expected_head`` (the ``head`` of the preview
    the user looked at) aborts with ``StaleView`` when the master moved in
    between (spec 11.2).  Conflicts without a stored decision raise
    ``Unresolved``; deferred ones are skipped and their sync state is left
    untouched, so they surface again next time (spec 4.4).  Committing the
    same batch twice is a no-op (spec 11.3).
    """
    if expected_head is not None and expected_head != base.head():
        raise StaleView("master is at version %s, preview was at %s"
                        % (base.head(), expected_head))
    d = diff(base, batch_id)
    source_id = d["source"]
    source = base.get_source(source_id)
    res = {(c["external_key"], c["role"], c["field"]): c
           for c in conflicts(base, batch_id)}
    open_ = [k for k, c in res.items() if c["resolution"] in (None, "") and not c["auto"]]
    if open_:
        raise Unresolved(open_)
    summary = dict(batch=batch_id, created=[], updated=[], same=[], vanished=[], deferred=[])
    with base.transaction(note or ("sync batch %d from %s" % (batch_id, source_id)),
                          kind="sync", batch_id=batch_id):
        for rec in d["records"]:
            key = rec["external_key"]
            touched = False
            for role in rec["roles"]:
                etype, rname = role["entity_type"], role["role"]
                if role["status"] == "new":
                    eid = role.get("candidate")
                    if eid is None:
                        eid = base.insert(etype, role["values"])
                    else:                       # pre-existing entity: only fill empty fields
                        cur = base.values(etype, eid)
                        base.update(etype, eid, {f: v for f, v in role["values"].items()
                                                 if cur.get(f) in ("", None)})
                    base.con.execute(
                        "INSERT INTO entity_links (source_id, external_key, role, entity_type,"
                        " entity_id, created_at) VALUES (?,?,?,?,?,?)",
                        (source_id, key, rname, etype, eid, now()))
                    m_now = base.values(etype, eid)
                    for f, v in role["values"].items():
                        _set_state(base, source_id, key, rname, f, v, m_now.get(f))
                        _decide(base, etype, eid, f, m_now.get(f), False, source_id)
                    summary["created"].append((key, rname, etype, eid))
                    touched = True
                    continue
                if role["status"] == "deleted_master":
                    continue
                eid = role["entity_id"]
                writes = {}
                for f, x in role["fields"].items():
                    r = res.get((key, rname, f))
                    if x["status"] == merge.CONFLICT:
                        rsl = r["resolution"] if r else None
                        if rsl == "defer":
                            summary["deferred"].append((key, rname, f))
                            continue
                        if rsl == "source":
                            writes[f] = x["s1"]
                            _decide(base, etype, eid, f, x["s1"], True, source_id, "conflict: source")
                        elif rsl == "master":
                            _decide(base, etype, eid, f, x["m1"], True, source_id, "conflict: master")
                        elif rsl == "value":
                            writes[f] = r["value"]
                            _decide(base, etype, eid, f, r["value"], True, source_id, "conflict: manual value")
                        elif x["apply"]:          # auto-resolved by the source_wins policy
                            writes[f] = x["value"]
                            _decide(base, etype, eid, f, x["value"], False, source_id, "policy: source_wins")
                    elif x["status"] == merge.SOURCE_UPDATE and x["apply"]:
                        writes[f] = x["value"]
                        _decide(base, etype, eid, f, x["value"], False, source_id)
                if writes:
                    base.update(etype, eid, writes)
                    touched = True
                m_now = base.values(etype, eid)
                for f, x in role["fields"].items():
                    r = res.get((key, rname, f))
                    if x["status"] == merge.NOT_PROVIDED:
                        continue
                    if r is not None and r["resolution"] == "defer":
                        continue                 # state frozen: surfaces again (spec 4.4)
                    if x["status"] == merge.MASTER_KEPT:
                        # master-only edit: observed, not reconciled (M0 stays)
                        _observe(base, source_id, key, rname, f, x["s1"], m_now.get(f))
                        continue
                    _set_state(base, source_id, key, rname, f, x["s1"], m_now.get(f))
            (summary["updated"] if touched else summary["same"]).append(key)
        for v in d["vanished"]:
            act = source.on_vanish
            if isinstance(act, (list, tuple)) and act[0] == "set":
                _, vrole, values = act
                for role, etype, eid in v["links"]:
                    if role == vrole:
                        base.update(etype, eid, dict(values))
            summary["vanished"].append(v["external_key"])
        base.con.execute(
            "UPDATE source_records SET accepted_at=? WHERE batch_id=?", (now(), batch_id))
        base.con.execute("UPDATE batches SET status='committed' WHERE id=?", (batch_id,))
    summary["head"] = base.head()
    return summary
