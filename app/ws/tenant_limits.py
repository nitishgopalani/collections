"""Phase C: per-tenant concurrency accounting for brain WebSocket sessions.

An in-process counter of active sessions per tenant, used to enforce a
per-tenant concurrency cap at session_start. This is deliberately in-process
(no DB / cross-node coordination) — cluster-wide caps are deferred (see
PR_NOTES). The single shared instance is ``SESSION_REGISTRY``.
"""

from __future__ import annotations

import threading


class TenantSessionRegistry:
    """Thread-safe active-session counter keyed by tenant_id."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def try_acquire(self, tenant_id: str, cap: int) -> bool:
        """Reserve a session slot for ``tenant_id``.

        ``cap <= 0`` means unlimited. Returns False (without reserving) when the
        tenant is already at its cap.
        """
        with self._lock:
            current = self._counts.get(tenant_id, 0)
            if cap > 0 and current >= cap:
                return False
            self._counts[tenant_id] = current + 1
            return True

    def release(self, tenant_id: str) -> None:
        """Release a previously-acquired slot; never drops below zero."""
        with self._lock:
            current = self._counts.get(tenant_id, 0)
            if current <= 1:
                self._counts.pop(tenant_id, None)
            else:
                self._counts[tenant_id] = current - 1

    def active(self, tenant_id: str) -> int:
        with self._lock:
            return self._counts.get(tenant_id, 0)

    def reset(self) -> None:
        """Clear all counters (test helper)."""
        with self._lock:
            self._counts.clear()


# Process-wide singleton shared by all brain WS connections.
SESSION_REGISTRY = TenantSessionRegistry()
