"""Prevent accidental direct host execution of repository Python tools.

Maintained launchers supply the marker when starting contained Python. The
container engine owns isolation; an environment value does not attest to it.
Importing this module alone has no effect, so callers choose the guard boundary.
"""

import os


def require_container():
    """Return only for the exact launcher marker; otherwise exit with guidance."""
    if os.environ.get("NMR_PROVIDER_TOOLS_IN_CONTAINER") != "1":
        raise SystemExit(
            "Cannot run repository Python without its container launcher. "
            "Use the maintained Make target so the launcher supplies the container environment."
        )
