"""install.sh: the address question ends up in .env as PUBLIC_BASE_URL, with the port when it isn't 80."""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent


def install(tmp_path, answers):
    for f in ("install.sh", ".env.example"):
        shutil.copy(ROOT / f, tmp_path)
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    for cmd, code in (("docker", 0), ("getent", 2)):  # docker does nothing; no name resolves
        (bin_ / cmd).write_text(f"#!/bin/sh\nexit {code}\n")
        (bin_ / cmd).chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", TZ="<IST>-5:30")
    out = subprocess.run(["bash", tmp_path / "install.sh"], input=answers, env=env,
                         capture_output=True, text=True, check=True).stdout
    return dict(line.split("=", 1) for line in (tmp_path / ".env").read_text().splitlines()
                if "=" in line and not line.startswith("#")), out


def test_ip_default_port(tmp_path):
    env, out = install(tmp_path, "1\n192.168.1.5\n\n")
    assert env["PUBLIC_BASE_URL"] == "http://192.168.1.5" and env["PORT"] == "80"
    assert env["TZ"] == "<IST>-5:30" and "http://192.168.1.5/phone" in out


def test_name_other_port(tmp_path):
    env, out = install(tmp_path, "2\ninv.home\n8000\n")
    assert env["PUBLIC_BASE_URL"] == "http://inv.home:8000" and env["PORT"] == "8000"
    assert "inv.home doesn't resolve yet" in out
    _, out = install(tmp_path, "")  # second run keeps the answers
    assert "Using existing .env" in out and "http://inv.home:8000/phone" in out
