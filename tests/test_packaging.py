"""Packaging metadata checks.

These guard the desktop-installer packaging, which is otherwise only exercised
on a Windows CI runner. The Inno Setup compiler resolves relative paths against
the directory containing the .iss script (``packaging/``), *not* against the
current working directory -- a mistake that silently produces paths like
``packaging/packaging/icon.ico`` and fails the build. Every path in the script
therefore goes through the ``{#RepoRoot}`` macro, and these tests verify that
each one resolves to a file that actually exists in the repository.
"""

import configparser
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGING = os.path.join(ROOT, "packaging")
ISS = os.path.join(PACKAGING, "windows_installer.iss")
DESKTOP = os.path.join(PACKAGING, "med17flasher.desktop")

# Paths produced by the build (PyInstaller output) rather than committed.
BUILD_OUTPUTS = ("dist/",)


def _iss_text():
    with open(ISS, "r", encoding="utf-8") as fh:
        return fh.read()


def _resolve(raw):
    """Resolve an .iss path the way Inno Setup does, returning an abs path.

    ``{#RepoRoot}`` is ``AddBackslash(SourcePath) + ".."`` -- i.e. the parent of
    the script's directory. Anything *without* the macro is resolved by Inno
    against the script's own directory, which is the bug these tests guard.
    """
    path = raw.strip().replace("\\", "/")
    if "{#RepoRoot}" in path:
        path = path.replace("{#RepoRoot}", "").lstrip("/")
        base = ROOT
    else:
        base = PACKAGING
    return os.path.normpath(os.path.join(base, path)), path


def _iss_source_paths():
    """Every filesystem path the compiler must be able to find."""
    text = _iss_text()
    found = []
    for m in re.finditer(r'^\s*Source:\s*"([^"]+)"', text, re.M):
        found.append(m.group(1))
    for key in ("SetupIconFile", "OutputDir"):
        m = re.search(r"^%s\s*=\s*(.+)$" % key, text, re.M)
        assert m, "%s missing from %s" % (key, ISS)
        found.append(m.group(1))
    return found


def test_iss_paths_resolve_inside_the_repo_root():
    """No path may resolve into packaging/ by accident (the CI failure)."""
    for raw in _iss_source_paths():
        resolved, rel = _resolve(raw)
        assert not resolved.startswith(PACKAGING + os.sep + "dist"), (
            "%r resolves to %r -- relative .iss paths are resolved against the "
            "script directory, use {#RepoRoot}" % (raw, resolved)
        )
        assert os.path.commonpath([resolved, ROOT]) == ROOT, (
            "%r escapes the repository (%r)" % (raw, resolved)
        )


def test_iss_source_files_exist():
    """Every [Files] Source and the SetupIconFile must exist in the checkout."""
    for raw in _iss_source_paths():
        resolved, rel = _resolve(raw)
        if any(rel.startswith(p) for p in BUILD_OUTPUTS):
            continue  # produced by PyInstaller during the build
        if any(ch in os.path.basename(resolved) for ch in "*?"):
            # A wildcard Source (e.g. the drivers\* payload): its parent
            # directory must exist so the compiler has something to scan.
            assert os.path.isdir(os.path.dirname(resolved)), (
                "%r -> %r: parent directory does not exist" % (raw, resolved)
            )
            continue
        assert os.path.exists(resolved), (
            "%r -> %r does not exist" % (raw, resolved)
        )


def test_iss_ships_the_pyinstaller_binary():
    """The installer must package the executable the spec actually builds."""
    text = _iss_text()
    m = re.search(r'#define\s+AppExeName\s+"([^"]+)"', text)
    assert m, "AppExeName not defined"
    assert m.group(1) == "med17flasher-desktop.exe"
    with open(os.path.join(PACKAGING, "med17flasher.spec"), encoding="utf-8") as fh:
        spec = fh.read()
    assert 'name="med17flasher-desktop"' in spec


def test_iss_appid_is_a_valid_guid():
    text = _iss_text()
    m = re.search(r"^AppId=\{\{(.+?)\}\}", text, re.M)
    assert m, "AppId missing"
    guid = m.group(1)
    assert re.fullmatch(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
        r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
        guid,
    ), "AppId %r is not a valid GUID" % guid


def test_spec_uses_the_icon_on_windows():
    with open(os.path.join(PACKAGING, "med17flasher.spec"), encoding="utf-8") as fh:
        spec = fh.read()
    assert "icon=app_icon" in spec, "EXE() must use the generated app icon"
    assert os.path.isfile(os.path.join(PACKAGING, "icon.ico"))
    assert os.path.isfile(os.path.join(PACKAGING, "icon.png"))


def test_linux_desktop_entry_is_valid():
    cp = configparser.RawConfigParser()
    cp.optionxform = str  # desktop entry keys are case sensitive
    with open(DESKTOP, encoding="utf-8") as fh:
        cp.read_file(fh)
    assert cp.has_section("Desktop Entry")
    entry = cp["Desktop Entry"]
    assert entry["Type"] == "Application"
    assert entry["Name"]
    # Exec must be the console script pyproject actually installs.
    assert entry["Exec"].split()[0] == "med17flasher-desktop"
    # Categories/Keywords are semicolon-terminated lists per the freedesktop spec.
    for key in ("Categories", "Keywords"):
        assert entry[key].endswith(";"), "%s must end with ';'" % key


def test_desktop_entry_exec_matches_pyproject_script():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        pyproject = fh.read()
    assert "med17flasher-desktop = " in pyproject


@pytest.mark.parametrize("script", ["install_linux.sh"])
def test_shell_installer_is_executable_and_sane(script):
    path = os.path.join(PACKAGING, script)
    assert os.path.isfile(path)
    assert os.access(path, os.X_OK), "%s must be executable" % script
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    assert body.startswith("#!"), "missing shebang"
    assert "set -eu" in body, "must fail fast"


def test_frozen_app_exposes_the_cli_subcommands():
    """The shipped executable must be usable without a Python install.

    A workshop that only has the .exe still needs the write-free pre-flight
    checks (`j2534`, `scan`) and `analyze-trace` - exactly the commands you run
    *before* touching a car. Without this passthrough the binary could only
    open the window.
    """

    from med17flasher.desktop import _run

    assert _run(["backends"]) == 0          # a real subcommand runs
    assert _run(["--selftest"]) in (0, 1)   # the selftest flag still wins


def test_frozen_app_cli_reports_failures_as_exit_codes():
    from med17flasher.desktop import _run

    # No PassThru device here, so this must fail cleanly rather than raise.
    assert _run(["j2534"]) == 1


def test_iss_has_tactrix_driver_autoinstall():
    """The installer must carry the bundled-driver auto-install hook so a
    Tactrix works right after setup (packaging/drivers/ -> silent install)."""
    text = _iss_text()
    assert "RunBundledDrivers" in text
    assert "installdriver" in text
    assert "HaveDriver" in text          # compile-time presence gate
    assert "ssPostInstall" in text       # runs after the app files land
    # the drop-in folder and its operator instructions must exist
    drivers = os.path.join(PACKAGING, "drivers")
    assert os.path.isdir(drivers)
    assert os.path.isfile(os.path.join(drivers, "README.txt"))
