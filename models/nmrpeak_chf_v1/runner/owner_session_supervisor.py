"""Compose the shared owner-session fence with the CHF runner endpoint."""

import nmrpeak_provider.owner_session_supervisor as _supervisor
from nmrpeak_provider.runner_protocol import RUNNER_SOCKET_PATH


if __name__ == "__main__":
    raise SystemExit(_supervisor.main(RUNNER_SOCKET_PATH))
