"""Integrity validation with pluggable business rules (spec 11.4)."""
from .store import q


def check(base, name, fn):
    """Register a business rule.  ``fn(base)`` returns a list of findings
    (any JSON-serializable items)."""
    base.checks[name] = fn


def validate(base):
    """Run the built-in checks plus every registered rule.

    Returns ``{check_name: [finding, ...]}`` with empty lists omitted.
    """
    out = {}
    dangling = []
    for r in base.con.execute("SELECT * FROM entity_links"):
        e = base.entities.get(r["entity_type"])
        if e is None:
            dangling.append("link %d: unknown entity type %s" % (r["id"], r["entity_type"]))
            continue
        hit = base.con.execute("SELECT 1 FROM %s WHERE %s=?" % (q(e.table), q(e.pk)),
                               (r["entity_id"],)).fetchone()
        if not hit:
            dangling.append("link %d: %s/%s does not exist"
                            % (r["id"], r["entity_type"], r["entity_id"]))
    if dangling:
        out["links"] = dangling
    for name, fn in base.checks.items():
        findings = fn(base)
        if findings:
            out[name] = list(findings)
    return out
