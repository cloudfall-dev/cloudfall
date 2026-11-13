"""Alert channels on hosts (S-015): stage each host's channel for ``baseline.yml``.

A server whose baseline names an ``AlertChannel`` gets the channel's sender,
chat id and bot token. The token is an input secret in the project's store
(S-011); it is decrypted here, written to a ``0600`` file in a private temp
dir, and handed to ``baseline.yml`` by path, so it never appears in an
extra var, a rendered inventory or a log. A token not in the store stops the
converge before any server is touched.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cloudfall.domain import ResourceKind
from cloudfall.requirements import (
    TELEGRAM_TOKEN_NAME,
    SecretRequirement,
    requirements_of,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cloudfall.domain import ResourceId
    from cloudfall.inventory import AlertChannelInventory, PlatformInventory
    from cloudfall.secret_store import SecretStore

CHANNELS_VARIABLE = "cloudfall_alert_channels"
"""``baseline.yml``'s variable: server id, then ``{id, chatId, tokenSource}``."""
_STAGED_MODE = 0o600


@dataclass(frozen=True, slots=True)
class HostChannel:
    """One server's channel and the requirement that holds its token."""

    server: ResourceId
    channel: AlertChannelInventory
    token: SecretRequirement


class TokenMissingError(RuntimeError):
    """A channel's bot token is not in the store; nothing ran."""

    def __init__(self, channels: tuple[str, ...]) -> None:
        """Name each channel whose token a person must supply."""
        super().__init__(
            "the bot token of alert channel(s) "
            f"{', '.join(channels)} is not in the secret store"
        )
        self.channels = channels


def host_channels(inventory: PlatformInventory) -> tuple[HostChannel, ...]:
    """Return every server whose baseline names a channel, with that channel."""
    channels = {item.resource_id: item for item in inventory.alert_channels}
    tokens = {
        (server.server_id.value, requirement.owner.resource_id.value): requirement.spec
        for server in requirements_of(inventory)
        for requirement in server.requirements
        if requirement.owner.kind is ResourceKind.ALERT_CHANNEL
        and isinstance(requirement.spec, SecretRequirement)
        and requirement.spec.name == TELEGRAM_TOKEN_NAME
    }
    found: list[HostChannel] = []
    for server in inventory.servers:
        channel_id = inventory.baseline(server.baseline_id).alert_channel
        if channel_id is None:
            continue
        found.append(
            HostChannel(
                server.resource_id,
                channels[channel_id],
                tokens[(server.resource_id.value, channel_id.value)],
            )
        )
    return tuple(found)


def require_tokens(hosts: tuple[HostChannel, ...], store: SecretStore) -> None:
    """Stop before any change when a channel's token is not stored."""
    missing = sorted(
        {
            host.channel.resource_id.value
            for host in hosts
            if not store.holds(host.token.reference)
        }
    )
    if missing:
        raise TokenMissingError(tuple(missing))


@contextmanager
def staged_channels(
    hosts: tuple[HostChannel, ...], store: SecretStore
) -> Iterator[dict[str, object]]:
    """Stage each token in a ``0600`` file; yield ``baseline.yml``'s extra vars."""
    with tempfile.TemporaryDirectory(prefix="cloudfall-alerting-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        channels: dict[str, dict[str, str]] = {}
        for host in hosts:
            source = root / f"{host.server.value}-{host.channel.resource_id.value}"
            descriptor = os.open(
                source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _STAGED_MODE
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(store.value(host.token.reference))
            channels[host.server.value] = {
                "id": host.channel.resource_id.value,
                "chatId": host.channel.telegram_chat_id.value,
                "tokenSource": str(source),
            }
        yield {CHANNELS_VARIABLE: channels}
