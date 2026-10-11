"""Ansible callback plugins the SDK enables on the playbooks it runs.

Ansible, not the SDK, imports what is in this directory: ``run_command``
points ``ANSIBLE_CALLBACK_PLUGINS`` at it. Ansible skips files whose name
starts with an underscore, so this one is never loaded as a plugin.
"""
