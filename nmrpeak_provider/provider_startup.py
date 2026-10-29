"""Admit the host-captured runtime inputs through the installed SDK parsers.

The host publishes an immutable, read-only tree with its existing named-user ACL
for the provider UID. These operator-owned snapshots deliberately use NMR's
bounded reader rather than the SDK default root/runtime-owned file policy.
SDK binding and credential admission remain the only format authorities.
"""

from dataclasses import dataclass, replace
from pathlib import Path

from provider_core.api_config import ApiSettings, api_settings
from provider_core.deployment_binding import DeploymentBinding, DeploymentBindings, parse_deployment_bindings
from provider_core.startup_file import StartupFileRejected

from .local_input import LocalInputFailureReason, LocalInputSnapshotError, read_bounded_regular_file
from .provider_config import BINDING_PATH, CONFIG_PATH, ProviderRuntimeConfig, api_input_target, decode_provider_runtime_config


@dataclass(frozen=True, slots=True)
class ProviderStartup:
    configured: ProviderRuntimeConfig
    definition: DeploymentBindings
    binding: DeploymentBinding
    settings: ApiSettings


def read_startup_input(path, *, maximum_bytes, label, secret=False):
    """Read a host-admitted snapshot; both secret and public files share its ACL.

    secret is the SDK reader callback's privacy classification. Source privacy,
    snapshot ownership and read-only grants are enforced by the host publisher;
    reading here never changes permissions or falls back to the original path.
    """
    try:
        return read_bounded_regular_file(Path(path), maximum_bytes=maximum_bytes)
    except LocalInputSnapshotError as cause:
        reason = {
            LocalInputFailureReason.UNREADABLE: "the captured file could not be read and closed successfully",
            LocalInputFailureReason.NOT_REGULAR: "the captured input is not a regular file",
            LocalInputFailureReason.TOO_LARGE: f"the captured file exceeds the {maximum_bytes}-byte limit",
        }[cause.reason]
        raise StartupFileRejected(f"Cannot read {label}: {reason}.", cause=cause) from None


def read_captured_api_input(path, *, maximum_bytes, label, secret=False):
    return read_startup_input(api_input_target(path), maximum_bytes=maximum_bytes, label=label, secret=secret)


def load_provider_startup(config_path=CONFIG_PATH):
    configured = decode_provider_runtime_config(read_startup_input(
        config_path, maximum_bytes=65536, label="NMRPeak runtime configuration",
    ))
    definition, binding, settings = load_captured_api_binding(
        configured.policy.binding_name, configured.api_proxy_socket,
        definition_path=configured.policy.binding_definition,
    )
    return ProviderStartup(configured, definition, binding, settings)


def load_captured_api_binding(binding_name, proxy_socket, *, definition_path=BINDING_PATH):
    """Return SDK definition, selected binding and settings without science input."""
    definition = parse_deployment_bindings(read_startup_input(
        definition_path, maximum_bytes=65536, label="NMRPeak captured API bindings",
    ))
    binding = definition.selected(binding_name)
    settings, _provider_ref = api_settings(binding.api, read_captured_api_input)
    settings = replace(settings, http=replace(settings.http, proxy_socket=proxy_socket))
    return definition, binding, settings
