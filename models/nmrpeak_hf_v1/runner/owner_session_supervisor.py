"""Launch the shared owner-session supervisor for the HF runner."""

from nmrpeak_provider import owner_session_supervisor as _supervisor
from nmrpeak_provider.runner_protocol import RUNNER_SOCKET_PATH


if __name__ == "__main__":
    raise SystemExit(_supervisor.main(RUNNER_SOCKET_PATH))
