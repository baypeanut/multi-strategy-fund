"""Backup validation with fake local transport; never reaches an SSH server."""

import io
import os
from pathlib import Path
import stat
import subprocess
import tarfile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/pull_offbox_backup.sh"


def run_backup(tmp_path, members):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for name, content in members:
            info = tarfile.TarInfo(name)
            data = content.encode()
            info.size = len(data)
            output.addfile(info, io.BytesIO(data))
    binaries = tmp_path / "fake-transport"
    binaries.mkdir()
    ssh = binaries / "ssh"
    ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_REMOTE_ARCHIVE"\n')
    scp = binaries / "scp"
    scp.write_text(
        '#!/bin/sh\nfor arg do target="$arg"; done\n/bin/cp "$FAKE_LOCAL_ARCHIVE" "$target"\n'
    )
    ssh.chmod(0o700)
    scp.chmod(0o700)
    destination = tmp_path / "backups"
    destination.mkdir(exist_ok=True)
    env = {
        "PATH": str(binaries) + os.pathsep + os.defpath,
        "HOME": str(tmp_path),
        "FUND_HOST": "backup.example.invalid",
        "FUND_REMOTE_DIR": "/srv/fund",
        "FUND_BACKUP_DIR": str(destination),
        "FAKE_REMOTE_ARCHIVE": "/srv/fund/data/backups/fund_ledgers_fixture.tar.gz",
        "FAKE_LOCAL_ARCHIVE": str(archive),
    }
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=10
    )
    return result, destination


@pytest.mark.parametrize("secret", [".env", ".env.production", "operator.env", "config.ini"])
def test_secret_guard_rejects_even_when_listing_continues_after_early_match(tmp_path, secret):
    members = [(secret, "not-a-real-secret"), ("data/state.json", "{}")]
    # A long listing reproduces the former tar | grep -q / pipefail failure.
    members.extend((f"padding/item_{i:05}.txt", "") for i in range(10000))
    result, destination = run_backup(tmp_path, members)
    assert result.returncode == 2 and "contains a secret" in result.stderr
    assert not list(destination.iterdir())


def test_valid_backup_is_private_and_does_not_follow_predictable_part_symlink(tmp_path):
    destination = tmp_path / "backups"
    destination.mkdir()
    untouched = tmp_path / "untouched.txt"
    untouched.write_text("preserve")
    (destination / "fund_ledgers_fixture.tar.gz.part").symlink_to(untouched)
    result, destination = run_backup(tmp_path, [("data/state.json", "{}")])
    assert result.returncode == 0, result.stderr
    assert untouched.read_text() == "preserve"
    saved = destination / "fund_ledgers_fixture.tar.gz"
    assert saved.exists() and stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert not list(destination.glob(".incoming.*"))


def test_backup_without_state_is_rejected_and_own_temp_is_removed(tmp_path):
    result, destination = run_backup(tmp_path, [("data/notes.json", "{}")])
    assert result.returncode == 3 and "no state.json" in result.stderr
    assert not list(destination.iterdir())
