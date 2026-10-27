"""Read-only freshness evidence, using B's bounded inventory and canonical epoch.

The epoch is maintained transactionally by migration 005 triggers, including
legacy SQL writers. A scan exception is an unavailable inventory, never empty.
Retrieval still rehydrates every excerpt and rechecks visibility before emit.
"""
from __future__ import annotations

from dataclasses import dataclass

from muninn.index import registry
from muninn.index.publisher import _inventory, PublishError


@dataclass(frozen=True)
class Freshness:
    source_epoch: int
    generation_epoch: int
    inventory_digest: str | None
    reason: str | None

    @property
    def current(self) -> bool:
        return self.reason is None


def assess(conn, manifest, namespaces=None) -> Freshness:
    """Fail closed for missing scopes, partial coverage, mutations or scan failure."""
    epoch = registry.source_epoch(conn)
    scope = tuple(namespaces or manifest.namespaces)
    reason = None
    digest = None
    if not set(scope).issubset(manifest.namespaces):
        reason = "scope_unavailable"
    elif epoch != manifest.source_epoch:
        reason = "stale_index"
    else:
        try:
            inventory = _inventory(conn, manifest.namespaces)
            digest = inventory.digest
            if (manifest.authored_inventory_digest is not None
                    and digest != manifest.authored_inventory_digest):
                reason = "stale_index"
        except (OSError, PublishError, ValueError):
            reason = "authored_inventory_unavailable"
    intentional = {"hidden", "symlink", "not_markdown", "not_visible", "excluded_symlink", "enrichment_withheld"}
    if reason is None:
        for omission in manifest.omissions:
            namespace = omission.get("namespace")
            if namespace is None and omission.get("path", "").startswith("wiki/"):
                namespace = omission["path"].split("/")[1]
            if omission.get("reason") not in intentional and (namespace is None or namespace in scope):
                reason = "incomplete_coverage"
                break
    return Freshness(epoch, manifest.source_epoch, digest, reason)
