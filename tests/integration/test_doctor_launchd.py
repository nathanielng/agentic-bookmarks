import plistlib
import subprocess

from bmk import doctor, launchd
from bmk.ingest.cloudtabs import run_import
from bmk.service import Library


def fake_runner(responses: dict[str, tuple[int, str]]):
    """launchctl stand-in: maps the subcommand ('print', 'bootstrap', …) to (returncode, stdout)."""
    calls = []

    def run(cmd):
        calls.append(cmd)
        code, out = responses.get(cmd[1], (0, ""))
        return subprocess.CompletedProcess(cmd, code, out, "" if code == 0 else "boom")

    run.calls = calls
    return run


def test_plist_runs_module_with_interval_and_env(config):
    config.poll_interval = 600
    data = plistlib.loads(launchd.render_plist(config, python="/venv/bin/python", env={"BMK_DISPLAY_TZ": "UTC", "HOME": "/x"}))
    assert data["Label"] == launchd.LABEL
    assert data["ProgramArguments"] == ["/venv/bin/python", "-m", "bmk", "ingest", "cloudtabs"]
    assert data["StartInterval"] == 600 and data["RunAtLoad"] is True
    assert data["EnvironmentVariables"] == {"BMK_DB_PATH": str(config.db_path), "BMK_DISPLAY_TZ": "UTC"}  # only BMK_*


def test_install_bootstraps_and_reports_status(config, tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "LOG_DIR", tmp_path / "Logs")
    plist = tmp_path / "LaunchAgents" / "com.bmk.cloudtabs.plist"
    run = fake_runner({"print": (0, "state = not running\n\tlast exit code = 0\n")})
    st = launchd.install(config, run=run, plist_path=plist)
    assert plist.exists() and st.installed and st.loaded and st.last_exit == 0
    assert [c[1] for c in run.calls] == ["bootout", "bootstrap", "print"]
    launchd.uninstall(run=run, plist_path=plist)
    assert not plist.exists()


def test_install_surfaces_bootstrap_failure(config, tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "LOG_DIR", tmp_path / "Logs")
    run = fake_runner({"bootstrap": (5, "")})
    try:
        launchd.install(config, run=run, plist_path=tmp_path / "x.plist")
    except RuntimeError as exc:
        assert "bootstrap failed" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_agent_check_flags_failed_last_run(monkeypatch, tmp_path):
    plist = tmp_path / "agent.plist"
    plist.write_text("x")
    monkeypatch.setattr(launchd, "PLIST_PATH", plist)
    check = doctor.check_agent(fake_runner({"print": (0, "last exit code = 1\n")}))
    assert check.status == doctor.FAIL and "Full Disk Access" in check.fix
    assert doctor.check_agent(fake_runner({"print": (0, "last exit code = 0\n")})).status == doctor.OK
    plist.unlink()
    assert doctor.check_agent(fake_runner({"print": (113, "")})).status == doctor.WARN


def test_doctor_reports_permission_denied(config, cloudtabs, monkeypatch, lib):
    import bmk.doctor as d
    from bmk.ingest import cloudtabs as ct

    def denied(explicit=None):
        raise ct.CloudTabsPermissionDenied(ct.CLOUDTABS_CANDIDATES[0])

    monkeypatch.setattr(d, "locate", denied)
    check = d.check_cloudtabs(config)
    assert check.status == doctor.FAIL and "Full Disk Access" in check.fix


def test_doctor_last_scan(config, cloudtabs, lib: Library):
    assert doctor.check_last_scan(config).status == doctor.WARN  # never run
    config.cloudtabs_path = cloudtabs.path
    cloudtabs.open_tab("T1", "https://example.com/", "E")
    run_import(lib, config)
    assert doctor.check_last_scan(config).status == doctor.OK
    lib.record_failed_run("cloudtabs", "Permission denied reading x.\nGrant Full Disk Access…")
    check = doctor.check_last_scan(config)
    assert check.status == doctor.FAIL and "Full Disk Access" in check.fix
