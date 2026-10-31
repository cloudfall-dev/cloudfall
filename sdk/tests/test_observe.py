"""Collecting observations through the team's own inventory."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cloudfall.ansible_api import InventorySource, read_inventory
from cloudfall.ansible_reader import read_fleet
from cloudfall.inventory import PlatformInventory
from cloudfall.observe import (
    OBSERVED_GROUP,
    OVERLAY_FILE,
    ObservationRequest,
    ObserveError,
    PlaybookRun,
    _run_playbook,
    collect_observations,
    observation_command,
    overlay_document,
    team_configuration,
    write_overlay,
)
from cloudfall.resources import default_engine_directory, default_schema_directory
from test_ansible_reader import _inventory

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

SCHEMAS = default_schema_directory()
ENGINE = default_engine_directory()


def _fleet(tmp_path: Path) -> tuple[PlatformInventory, InventorySource]:
    source = _inventory(tmp_path)
    read = read_fleet(read_inventory(source), SCHEMAS)
    return PlatformInventory.from_state(read.config), source


def _request(
    tmp_path: Path, source: InventorySource, **changes: object
) -> ObservationRequest:
    defaults: dict[str, object] = {
        "inventory_sources": (source.value,),
        "output_directory": tmp_path / "observed",
        "engine_directory": ENGINE,
    }
    defaults.update(changes)
    return ObservationRequest(**defaults)  # type: ignore[arg-type]


def test_the_overlay_carries_only_what_ansible_cannot_know(tmp_path: Path) -> None:
    """Connection settings stay in the team's inventory, not in the overlay."""
    fleet, _ = _fleet(tmp_path)

    document = overlay_document(fleet)

    hosts = document["all"]["children"][OBSERVED_GROUP]["hosts"]  # type: ignore[index]
    assert sorted(hosts) == ["web-1", "web-2"]
    assert sorted(hosts["web-1"]) == ["cloudfall_server_id", "cloudfall_server_type"]
    assert hosts["web-1"]["cloudfall_server_id"] == "web-1"
    assert hosts["web-1"]["cloudfall_server_type"]["id"] == "web"


def test_ansible_merges_the_overlay_over_the_team_inventory(tmp_path: Path) -> None:
    """The whole approach rests on this: their inventory keeps deciding."""
    fleet, source = _fleet(tmp_path)
    overlay = write_overlay(fleet, tmp_path / "overlay")

    listing = subprocess.run(  # noqa: S603 - fixed binary, test paths.
        [  # noqa: S607
            "ansible-inventory",
            "--inventory",
            str(source),
            "--inventory",
            str(overlay),
            "--list",
        ],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )

    parsed = json.loads(listing.stdout)
    assert sorted(parsed[OBSERVED_GROUP]["hosts"]) == ["web-1", "web-2"]
    host_vars = parsed["_meta"]["hostvars"]["web-1"]
    assert host_vars["ansible_host"] == "10.0.0.1"
    assert host_vars["ansible_user"] == "ansible"
    # The overlay's strings stay unsafe in Ansible, never templated (#174).
    assert host_vars["cloudfall_server_id"] == {"__ansible_unsafe": "web-1"}
    assert host_vars["cloudfall_server_type"]["id"] == {"__ansible_unsafe": "web"}
    assert "cloudfall" in host_vars


def test_the_overlay_is_written_where_it_is_asked_for(tmp_path: Path) -> None:
    fleet, _ = _fleet(tmp_path)

    overlay = write_overlay(fleet, tmp_path / "state")

    assert overlay == tmp_path / "state" / OVERLAY_FILE
    assert json.loads(overlay.read_text(encoding="utf-8"))["all"]["children"]


def test_the_command_passes_both_sources_and_the_output_directory(
    tmp_path: Path,
) -> None:
    fleet, source = _fleet(tmp_path)
    overlay = write_overlay(fleet, tmp_path / "overlay")
    request = _request(tmp_path, source, limit="web-1")

    argv, environment = observation_command(request, overlay)

    assert argv[0].endswith("ansible-playbook")
    assert argv.count("--inventory") == 2
    assert str(source) in argv
    assert str(overlay) in argv
    assert argv[argv.index("--limit") + 1] == "web-1"
    extra_vars = json.loads(argv[argv.index("--extra-vars") + 1])
    assert extra_vars == {
        "cloudfall_inspect_output_directory": str(tmp_path / "observed")
    }
    assert argv[-1].endswith("inspect.yml")
    assert Path(environment["ANSIBLE_ROLES_PATH"]).is_dir()


def test_the_team_configuration_decides_how_ansible_connects(
    tmp_path: Path,
) -> None:
    """Their `ansible.cfg` is honoured; only the roles path is overridden."""
    fleet, source = _fleet(tmp_path)
    overlay = write_overlay(fleet, tmp_path / "overlay")
    configuration = tmp_path / "ansible.cfg"
    configuration.write_text("[defaults]\nroles_path = roles\n", encoding="utf-8")

    _, environment = observation_command(
        _request(tmp_path, source, configuration=configuration), overlay
    )

    assert environment["ANSIBLE_CONFIG"] == str(configuration)
    assert Path(environment["ANSIBLE_ROLES_PATH"]) == (ENGINE / "ansible" / "roles")


def test_team_configuration_is_found_only_where_it_exists(tmp_path: Path) -> None:
    assert team_configuration(tmp_path) is None
    (tmp_path / "ansible.cfg").write_text("[defaults]\n", encoding="utf-8")
    assert team_configuration(tmp_path) == tmp_path / "ansible.cfg"


def test_a_relative_output_directory_is_refused(tmp_path: Path) -> None:
    """The inspect role writes on the controller and requires an absolute path."""
    _, source = _fleet(tmp_path)

    with pytest.raises(ObserveError) as error:
        _request(tmp_path, source, output_directory=Path("tmp/observed"))

    assert error.value.code == "observe_invalid_argument"


def test_a_missing_inventory_source_is_refused(tmp_path: Path) -> None:
    _, source = _fleet(tmp_path)

    with pytest.raises(ObserveError) as error:
        _request(tmp_path, source, inventory_sources=(tmp_path / "absent",))

    assert error.value.code == "observe_inventory_missing"


def test_a_run_reports_which_servers_it_observed(tmp_path: Path) -> None:
    """A snapshot that did not appear is reported rather than assumed."""
    fleet, source = _fleet(tmp_path)
    request = _request(tmp_path, source)
    recorded: list[tuple[str, ...]] = []

    def run(argv: Sequence[str], _environment: Mapping[str, str]) -> PlaybookRun:
        recorded.append(tuple(argv))
        request.output_directory.mkdir(parents=True, exist_ok=True)
        (request.output_directory / "web-1.json").write_text("{}", encoding="utf-8")
        return PlaybookRun(exit_code=0, output="")

    result = collect_observations(fleet, request, tmp_path / "overlay", run)

    assert len(recorded) == 1
    assert result.requested == ("web-1", "web-2")
    assert result.observed == ("web-1",)
    assert result.missing == ("web-2",)
    assert result.complete is False
    assert result.as_dict()["status"] == "incomplete"


def test_a_complete_run_is_reported_as_such(tmp_path: Path) -> None:
    fleet, source = _fleet(tmp_path)
    request = _request(tmp_path, source)

    def run(_argv: Sequence[str], _environment: Mapping[str, str]) -> PlaybookRun:
        request.output_directory.mkdir(parents=True, exist_ok=True)
        for server in ("web-1", "web-2"):
            (request.output_directory / f"{server}.json").write_text(
                "{}", encoding="utf-8"
            )
        return PlaybookRun(exit_code=0, output="")

    result = collect_observations(fleet, request, tmp_path / "overlay", run)

    assert result.complete is True
    assert result.observed == ("web-1", "web-2")
    assert result.missing == ()


def test_a_failed_playbook_keeps_its_exit_code(tmp_path: Path) -> None:
    fleet, source = _fleet(tmp_path)
    request = _request(tmp_path, source)

    recap = "PLAY RECAP web-1 unreachable=1"
    log = "PLAY [Inspect]\n" + "TASK [Gathering Facts]\n" * 200 + recap
    result = collect_observations(
        fleet,
        request,
        tmp_path / "overlay",
        lambda _argv, _env: PlaybookRun(exit_code=4, output=log),
    )

    assert result.exit_code == 4
    assert result.complete is False
    detail = result.as_dict()["detail"]
    assert isinstance(detail, str)
    assert len(detail) <= 2000
    assert detail.endswith(recap)


def test_a_successful_run_keeps_no_detail(tmp_path: Path) -> None:
    fleet, source = _fleet(tmp_path)
    request = _request(tmp_path, source)

    result = collect_observations(
        fleet,
        request,
        tmp_path / "overlay",
        lambda _argv, _env: PlaybookRun(exit_code=0, output="PLAY RECAP ok"),
    )

    assert "detail" not in result.as_dict()


def test_the_playbook_writes_nothing_to_the_cli_streams(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Ansible's output is captured: stdout carries the JSON document only."""
    script = (
        "import sys\n"
        "print('PLAY', flush=True)\n"
        "print('fatal', file=sys.stderr, flush=True)\n"
        "print('RECAP', flush=True)\n"
    )

    run = _run_playbook((sys.executable, "-c", script), {})

    captured = capfd.readouterr()
    assert (captured.out, captured.err) == ("", "")
    assert run.exit_code == 0
    assert run.output.split() == ["PLAY", "fatal", "RECAP"]


def test_a_limited_run_is_judged_against_the_hosts_it_asked_for(
    tmp_path: Path,
) -> None:
    """Narrowing a run on purpose must not report it as incomplete."""
    fleet, source = _fleet(tmp_path)
    request = _request(tmp_path, source, limit="web-2")

    def run(_argv: Sequence[str], _environment: Mapping[str, str]) -> PlaybookRun:
        request.output_directory.mkdir(parents=True, exist_ok=True)
        (request.output_directory / "web-2.json").write_text("{}", encoding="utf-8")
        return PlaybookRun(exit_code=0, output="")

    result = collect_observations(fleet, request, tmp_path / "overlay", run)

    assert result.requested == ("web-2",)
    assert result.observed == ("web-2",)
    assert result.missing == ()
    assert result.complete is True


def test_a_limit_matching_no_declared_server_is_refused(tmp_path: Path) -> None:
    fleet, source = _fleet(tmp_path)
    request = _request(tmp_path, source, limit="build-1")

    with pytest.raises(ObserveError) as error:
        collect_observations(
            fleet,
            request,
            tmp_path / "overlay",
            lambda _argv, _env: PlaybookRun(exit_code=0, output=""),
        )

    assert error.value.code == "observe_no_targets"
    assert "build-1" in error.value.detail


def test_the_wrapper_narrows_a_read_with_an_ansible_pattern(tmp_path: Path) -> None:
    source = _inventory(tmp_path)

    everything = read_inventory(source)
    narrowed = read_inventory(source, limit="app_servers:!web-2")

    assert [str(host.name) for host in everything.hosts] == [
        "build-1",
        "web-1",
        "web-2",
    ]
    assert [str(host.name) for host in narrowed.hosts] == ["web-1"]
