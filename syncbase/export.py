"""CSV export with diff preview and run history (spec 7, 8.4).

Export is one-way (exported CSV is never read back, spec 7.1), so it uses a
plain two-way comparison between the candidate rows and the last *confirmed*
run of the same profile.  Preparing (preview) never moves the baseline
(spec 7.5); only ``confirm_export`` records a run as official.
"""
import csv
import hashlib
import io
import json
import os

from .store import now


def export_profile(base, profile_id, query, key, columns=None, order_by=None,
                   encoding="utf-8", dialect="excel", ignore_order=True):
    """Declare (or update) an export profile (spec 7.2).

    ``query`` is a SELECT over the master tables; ``key`` is the list of
    column names that identify a row across runs; ``columns`` restricts and
    orders the output columns (default: all query columns).
    """
    base.con.execute(
        "INSERT INTO export_profiles (profile_id, definition, created_at) VALUES (?,?,?)"
        " ON CONFLICT(profile_id) DO UPDATE SET definition=excluded.definition",
        (profile_id, json.dumps(dict(query=query, key=list(key), columns=columns,
                                     order_by=order_by, encoding=encoding, dialect=dialect,
                                     ignore_order=ignore_order)), now()))
    base.con.commit()


def _profile(base, profile_id):
    r = base.con.execute("SELECT definition FROM export_profiles WHERE profile_id=?",
                         (profile_id,)).fetchone()
    if not r:
        raise KeyError("unknown export profile: %s" % profile_id)
    return json.loads(r["definition"])


def _rows(base, p):
    sql = p["query"] + ((" ORDER BY " + p["order_by"]) if p.get("order_by") else "")
    cur = base.con.execute(sql)
    cols = p.get("columns") or [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip([x[0] for x in cur.description], r))
        out.append({c: d.get(c) for c in cols})
    return cols, out


def _key_of(p, row):
    return json.dumps([row.get(k) for k in p["key"]], ensure_ascii=False)


def _last_confirmed(base, profile_id):
    return base.con.execute(
        "SELECT * FROM export_runs WHERE profile_id=? AND status='confirmed'"
        " ORDER BY id DESC LIMIT 1", (profile_id,)).fetchone()


def _run_rows(base, run_id):
    return {r["record_key"]: json.loads(r["row"]) for r in base.con.execute(
        "SELECT * FROM export_records WHERE run_id=?", (run_id,))}


def prepare_export(base, profile_id):
    """Generate candidate rows and snapshot them as a *prepared* run.

    Returns the run id; use ``export_diff`` to preview and
    ``confirm_export`` to make it official.

    ``db_version`` is the latest confirmed commit.  Inside an open
    ``transaction()`` it joins that transaction; it refuses when the
    transaction has already written master changes, because the rows would
    then match no confirmed version.
    """
    if base._dirty():
        raise RuntimeError("prepare_export inside a transaction with unconfirmed master changes:"
                           " the snapshot would match no confirmed version; export after it commits")
    p = _profile(base, profile_id)
    cols, rows = _rows(base, p)
    cur = base.con.execute(
        "INSERT INTO export_runs (profile_id, status, prepared_at, actor, db_version, row_count)"
        " VALUES (?,?,?,?,?,?)",
        (profile_id, "prepared", now(), base.actor, base.head(), len(rows)))
    run_id = cur.lastrowid
    for i, row in enumerate(rows):
        base.con.execute(
            "INSERT INTO export_records (run_id, record_key, row) VALUES (?,?,?)",
            (run_id, _key_of(p, row), json.dumps(dict(row, _pos=i), ensure_ascii=False)))
    base._autocommit()
    return run_id


def export_diff(base, run_id):
    """Differences between a run and the last confirmed run of its profile
    (spec 7.3): added, removed, changed rows and changed fields."""
    run = base.con.execute("SELECT * FROM export_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise KeyError("unknown export run: %s" % run_id)
    p = _profile(base, run["profile_id"])
    new = _run_rows(base, run_id)
    prev_run = base.con.execute(
        "SELECT * FROM export_runs WHERE profile_id=? AND status='confirmed' AND id<?"
        " ORDER BY id DESC LIMIT 1", (run["profile_id"], run_id)).fetchone()
    old = _run_rows(base, prev_run["id"]) if prev_run else {}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = []
    for k in set(new) & set(old):
        a, b = dict(old[k]), dict(new[k])
        if p.get("ignore_order", True):
            a.pop("_pos", None), b.pop("_pos", None)
        fields = [dict(field=f, old=a.get(f), new=b.get(f))
                  for f in b if a.get(f) != b.get(f) and f != "_pos"]
        if fields or a.get("_pos") != b.get("_pos"):
            changed.append(dict(key=k, fields=fields))
    return dict(run=run_id, against=prev_run["id"] if prev_run else None,
                added=added, removed=removed, changed=changed,
                same=len(set(new) & set(old)) - len(changed))


def _formula_safe(v):
    """Guard against CSV formula injection (spec 11.5)."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def confirm_export(base, run_id, path):
    """Write the CSV file and record the run as the new official output
    (spec 7.5).  Returns ``{file, hash, rows}``."""
    run = base.con.execute("SELECT * FROM export_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise KeyError("unknown export run: %s" % run_id)
    if run["status"] == "confirmed":
        raise RuntimeError("run %s is already confirmed" % run_id)
    p = _profile(base, run["profile_id"])
    rows = sorted(_run_rows(base, run_id).values(), key=lambda r: r.get("_pos", 0))
    cols = p.get("columns")
    if not cols:
        cols = [c for c in (rows[0] if rows else {}) if c != "_pos"]
    buf = io.StringIO()
    w = csv.writer(buf, dialect=p.get("dialect", "excel"))
    w.writerow(cols)
    for r in rows:
        w.writerow([_formula_safe(r.get(c)) for c in cols])
    data = buf.getvalue().encode(p.get("encoding", "utf-8"))
    with open(path, "wb") as f:
        f.write(data)
    digest = hashlib.sha256(data).hexdigest()
    base.con.execute(
        "UPDATE export_runs SET status='confirmed', confirmed_at=?, file_name=?, file_hash=?"
        " WHERE id=?", (now(), os.path.basename(path), digest, run_id))
    base.con.commit()
    return dict(file=path, hash=digest, rows=len(rows))


def run_rows(base, run_id):
    """The rows of a run, in output order, without the internal ``_pos``."""
    rows = sorted(_run_rows(base, run_id).values(), key=lambda r: r.get("_pos", 0))
    return [{k: v for k, v in r.items() if k != "_pos"} for r in rows]


def export_history(base, profile_id):
    """All runs of a profile, newest first (spec 7.6)."""
    return [dict(r) for r in base.con.execute(
        "SELECT * FROM export_runs WHERE profile_id=? ORDER BY id DESC", (profile_id,))]


def compare_exports(base, run_id_1, run_id_2):
    """Field-level differences between any two runs (spec 7.6)."""
    a, b = _run_rows(base, run_id_1), _run_rows(base, run_id_2)
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    changed = []
    for k in set(a) & set(b):
        fields = [dict(field=f, old=a[k].get(f), new=b[k].get(f))
                  for f in b[k] if f != "_pos" and a[k].get(f) != b[k].get(f)]
        if fields:
            changed.append(dict(key=k, fields=fields))
    return dict(added=added, removed=removed, changed=changed)
