"""Check launcher policy; actual isolation belongs to the container composition."""

import os


def require_container():
    """Reject accidental host execution without depending on a repository checkout."""
    if os.environ.get("PROVIDER_CORE_IN_CONTAINER") != "1":
        raise SystemExit(
            "Cannot run provider-core outside its contained launcher. Use the consumer's maintained container target."
        )
