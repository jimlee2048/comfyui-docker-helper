"""Stable BuildKit mounts for default OpenSSH host trust."""

from typing import Literal, NamedTuple


class KnownHostsMount(NamedTuple):
    """One default known-hosts source and its BuildKit mount identity."""

    secret_id: str
    target: str
    default_source: str
    scope: Literal["user", "system"]


KNOWN_HOSTS_MOUNTS: tuple[KnownHostsMount, ...] = (
    KnownHostsMount(
        secret_id="cdh-ssh-known-hosts-user",
        target="/run/secrets/cdh-ssh-known-hosts-user",
        default_source="~/.ssh/known_hosts",
        scope="user",
    ),
    KnownHostsMount(
        secret_id="cdh-ssh-known-hosts-user-legacy",
        target="/run/secrets/cdh-ssh-known-hosts-user-legacy",
        default_source="~/.ssh/known_hosts2",
        scope="user",
    ),
    KnownHostsMount(
        secret_id="cdh-ssh-known-hosts-system",
        target="/run/secrets/cdh-ssh-known-hosts-system",
        default_source="/etc/ssh/ssh_known_hosts",
        scope="system",
    ),
    KnownHostsMount(
        secret_id="cdh-ssh-known-hosts-system-legacy",
        target="/run/secrets/cdh-ssh-known-hosts-system-legacy",
        default_source="/etc/ssh/ssh_known_hosts2",
        scope="system",
    ),
)
