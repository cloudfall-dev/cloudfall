"""``domains apply --issue-certificate``: the role's side (spec S-004).

Real issuance needs Let's Encrypt and a public name, so the ``when``
conditions of the certificate tasks are evaluated with Ansible's own
templating, and the receipt tasks run for real twice on localhost.
"""

from __future__ import annotations

from pathlib import Path

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from test_domains_one_domain import RENDERED, _run, _sha256

ROLES = Path(__file__).parents[2] / "engine" / "ansible" / "roles"
ROLE_TASKS = ROLES / "cloudfall_nginx_site" / "tasks" / "main.yml"


def _when(name: str, variables: dict[str, object]) -> bool:
    """Return whether the named task of the role runs under ``variables``."""
    tasks = DataLoader().load_from_file(str(ROLE_TASKS))
    assert isinstance(tasks, list)
    (task,) = (task for task in tasks if task["name"] == name)
    conditions = task["when"]
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(
        templar.evaluate_conditional(trust_as_template(condition))
        for condition in (conditions if isinstance(conditions, list) else [conditions])
    )


def _variables(*, certificate_exists: bool) -> dict[str, object]:
    return {
        "ansible_check_mode": False,
        "cloudfall_nginx_site_issue_certificate": True,
        "cloudfall_nginx_site_domain": {"tls": {"mode": "required"}},
        "cloudfall_nginx_site_certificate": {"stat": {"exists": certificate_exists}},
    }


# proves: S-004-9 (issuance runs on a fresh Domain, and the timer is enabled)
def test_s004_9_a_fresh_domain_issues_and_enables_the_renewal_timer() -> None:
    fresh = _variables(certificate_exists=False)

    assert _when("Issue the declared certificate", fresh)
    assert _when("Enable the certificate renewal timer", fresh)


def test_s004_10_a_second_run_skips_issuance() -> None:
    issued = _variables(certificate_exists=True)

    assert not _when("Issue the declared certificate", issued)
    assert _when("Enable the certificate renewal timer", issued)


def test_s004_10_a_second_run_records_the_vhost_it_rendered_before(
    tmp_path: Path,
) -> None:
    """The second render writes what the first left, so the hashes agree."""
    vhost = tmp_path / "crm.example.test.conf"

    first = _run(tmp_path, vhost)
    second = _run(tmp_path, vhost)

    assert first["previousConfigurationSha256"] is None
    assert second["previousConfigurationSha256"] == _sha256(RENDERED)
    assert second["configurationSha256"] == second["previousConfigurationSha256"]
