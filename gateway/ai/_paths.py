"""Portable gateway-path resolution (LED-1715).

The gateway repo root used to be hardcoded as the founder's dev path
(``/home/delimit/delimit-gateway``) in ~8 functional defaults. That works on
the founder's machine (the path exists) but breaks on customer installs.

This module resolves the gateway repo root portably:
  1. An explicit env override (``DELIMIT_GATEWAY_REPO`` or the older
     ``DELIMIT_GATEWAY_ROOT`` alias) wins, for containers/CI runners that
     relocate the checkout.
  2. Otherwise, fall back to ``__file__``-relative resolution: this file lives
     at ``<gateway repo root>/ai/_paths.py``, so ``parent.parent`` is the repo
     root on ANY machine. On the founder's box this still resolves to
     ``/home/delimit/delimit-gateway`` — zero behavior change.

Keep this module dependency-free (stdlib only) so it can be imported from
anywhere without circular-import risk.
"""

import os
from pathlib import Path

# Env var names honored, in priority order. DELIMIT_GATEWAY_REPO is the
# canonical name used by content_grounding; DELIMIT_GATEWAY_ROOT is the alias
# used by continuity.py / inbox_daemon.py (LED-2107). Both are preserved.
_ENV_VARS = ("DELIMIT_GATEWAY_REPO", "DELIMIT_GATEWAY_ROOT")


def gateway_repo() -> str:
    """Return the gateway repo root as a string, portably."""
    for var in _ENV_VARS:
        val = os.environ.get(var)
        if val:
            return val
    # ai/_paths.py -> ai/ -> <gateway repo root>
    return str(Path(__file__).resolve().parent.parent)


GATEWAY_REPO = gateway_repo()
