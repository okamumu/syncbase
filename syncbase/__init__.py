"""syncbase — sync external data into a master SQLite database, git-style.

A small, dependency-free framework (Python standard library only) that
combines Dolt-style versioning (snapshot, commit, diff, three-way merge,
conflict detection, history) with MDM-style golden-record management
(source records, entity resolution, record linking, provenance, human
override).  See docs/design.md for the design and docs/spec.ja.md for the
original specification.

Typical flow::

    import syncbase

    base = syncbase.Base("master.sqlite")
    base.entity("book", fields=["title", "price"])
    base.source("shop", kind="full")

    batch = base.stage("shop", {"b1": {"title": "Dune", "price": "9"}},
                       mapping={"title": ("book", "", "title"),
                                "price": ("book", "", "price")})
    preview = base.diff(batch)          # read-only classification
    base.resolve(batch, {("b1", "title"): "master"})   # if conflicts
    base.commit(batch, expected_head=preview["head"])
"""
from . import export as _export
from . import links as _links
from . import relations as _relations
from . import provenance as _provenance
from . import staging as _staging
from . import validate as _validate
from . import versioning as _versioning
from .merge import (AGREED, CONFLICT, DELETE, MASTER_KEPT, MISSING,
                    NOT_PROVIDED, SOURCE_UPDATE, UNCHANGED,
                    three_way, three_way_field)
from .staging import StaleView, Unresolved
from .store import Base

__version__ = "0.1.0"
__all__ = ["Base", "StaleView", "Unresolved", "MISSING", "DELETE",
           "three_way", "three_way_field",
           "UNCHANGED", "NOT_PROVIDED", "MASTER_KEPT", "SOURCE_UPDATE",
           "AGREED", "CONFLICT"]

# Chapter-8 API, attached to Base as methods.
Base.stage = _staging.stage
Base.diff = _staging.diff
Base.conflicts = _staging.conflicts
Base.resolve = _staging.resolve
Base.commit = _staging.commit

Base.history = _versioning.history
Base.commits = _versioning.commits
Base.get_version = _versioning.get_version
Base.compare_versions = _versioning.compare_versions
Base.revert = _versioning.revert
Base.revert_change = _versioning.revert_change

Base.match_candidates = _links.match_candidates
Base.link = _links.link
Base.unlink = _links.unlink
Base.links_of = _links.links_of
Base.merge_entities = _links.merge_entities
Base.split_entity = _links.split_entity

Base.relate = _relations.relate
Base.unrelate = _relations.unrelate
Base.relation_of = _relations.relation_of
Base.relations = _relations.relations
Base.judge = _relations.judge
Base.judgements = _relations.judgements
Base.pair_verdicts = _relations.pair_verdicts

Base.provenance = _provenance.provenance
Base.decide = _provenance.decide

Base.check = _validate.check
Base.validate = _validate.validate

Base.export_profile = _export.export_profile
Base.prepare_export = _export.prepare_export
Base.export_diff = _export.export_diff
Base.confirm_export = _export.confirm_export
Base.export_history = _export.export_history
Base.compare_exports = _export.compare_exports
