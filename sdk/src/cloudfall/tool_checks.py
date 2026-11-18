"""The external tools ``doctor`` checks on ``cloudfall`` and ``cloudfall-engine``.

Both treaty apps declare these, so each ``doctor`` reads a tool's version
the same way.
"""

from __future__ import annotations

from treaty import Dependency

ANSIBLE_MINIMUM_VERSION = "2.21.0"
ANSIBLE_PLAYBOOK = Dependency(
    "ansible-playbook",
    check_command=("ansible-playbook", "--version"),
    min_version=ANSIBLE_MINIMUM_VERSION,
    fix_command=f"install ansible-playbook {ANSIBLE_MINIMUM_VERSION} or newer",
    # Only ansible's own first line counts: a traceback from an ansible that
    # fails at start names the Python version, which is no ansible version.
    version_regex=r"(?m)^ansible-playbook \[core (\d+(?:\.\d+)+)[^\]]*\]",
)
"""``doctor``'s check of ``ansible-playbook``, which the generic check of a
``required_tools`` entry would read from the first dotted number printed."""
