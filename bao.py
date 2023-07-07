#!/usr/bin/env python3.11
import sys

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 required")

import shutil
import subprocess
import tempfile
import tomllib

from pathlib import Path
from dataclasses import dataclass

ROOT_PATH = Path(".").resolve()
APPS_ROOT_PATH = ROOT_PATH / "apps"
CADDYFILES_PATH = ROOT_PATH / "caddyfiles"


SYSTEMCTL_RESTART_INTERVAL = 3


def get_systemctl_config(web_cmd: str, working_directory: str, description: str):
    return f"""
[Unit]
Description={description}

[Service]
After=network.target
Restart=always
RestartSec={SYSTEMCTL_RESTART_INTERVAL}
WorkingDirectory={working_directory}
ExecStart={web_cmd}

[Install]
WantedBy=multi-user.target
""".strip()


@dataclass
class Procfile:
    web_cmd: str


def parse_procfile(content: str):
    web_cmd = None
    for line in content.splitlines():
        if line.startswith("web:"):
            web_cmd = line.split(":", maxsplit=1)[-1].strip()

    if not web_cmd.startswith("python"):
        print("web cmd should start with python")
        sys.exit(1)

    if "$PORT" not in web_cmd:
        print("$PORT is not present on web cmd")
        sys.exit(1)

    return Procfile(web_cmd=web_cmd)


def get_app_caddyfile_config(domain: str, app_root_src_path: str, port: int):
    return (
        """
{domain} {
    reverse_proxy localhost:{port}

    handle_path /static/* {
        file_server {
            root {app_root_src_path}/static
        }
    }
}
""".strip()
        .replace("{domain}", domain)
        .replace("{app_root_src_path}", str(app_root_src_path))
        .replace("{port}", str(port))
    )


@dataclass
class BaoConfigApp:
    domain: str
    procfile: str = "Procfile"


@dataclass
class BaoConfig:
    """bao.toml"""

    apps: dict[str, BaoConfigApp]


def configure_app(app_name: str, tmp_path: Path):
    """
    apps/
        <appname>/
            root_src/ (source code)
            git/ (git bare repo)
            <appname>.service
            Caddyfile

    """
    if not (tmp_path / "bao.toml").exists():
        print("bao.toml not detected")
        sys.exit()

    if not (tmp_path / "pyproject.toml").exists():
        print("pyproject.toml not detected")
        sys.exit(1)

    if not (tmp_path / "Procfile").exists():
        print("Procfile not detected")
        sys.exit(1)

    # -- parse project config
    with open(tmp_path / "bao.toml") as f:
        bao_config = BaoConfig(**tomllib.load(f))

    app_config = bao_config.apps.get(app_name)
    if not app_config:
        print(f"{app_name} not found in bao config")
        sys.exit(1)

    with open(app_config.procfile) as f:
        procfile = parse_procfile(f.read())

    app_path = APPS_ROOT_PATH / app_name
    app_path.mkdir(parents=True, exist_ok=True)
    app_root_src_path = app_path / "root_src"
    # -- move files to our domain
    shutil.move(tmp_path, app_root_src_path)

    # -- configure poetry
    subprocess.run(["poetry", "install"], cwd=app_root_src_path, check=True)
    subprocess.run([str(app_root_src_path / ".venv/bin/python"), "-V"], check=True)
    # TODO: remove check=True and do proper validation and printing

    # -- configure systemctl
    web_cmd = procfile.web_cmd
    app_port = 8000
    # TODO: look for ports
    web_cmd = web_cmd.replace("$PORT", str(app_port))
    print(f"will use the following web cmd: {web_cmd!r}")

    systemctl_config = get_systemctl_config(
        web_cmd=f"{app_root_src_path / '.venv/bin'}/{web_cmd}",
        working_directory=app_root_src_path,
        description=f"{app_name} configured by bao",
    )
    service_name = f"{app_name}.service"
    with open(app_path / service_name, "w") as f:
        f.write(systemctl_config)

    subprocess.run(
        [
            "sudo",
            "ln",
            "-sf",
            str(app_path / service_name),
            f"/etc/systemd/system/{app_name}.service",
        ],
        check=True,
    )

    subprocess.run(["sudo", "systemctl", "enable", service_name], check=True)

    # -- configure caddy
    app_caddy_config = get_app_caddyfile_config(
        domain="test.adautomator.com",
        app_root_src_path=app_root_src_path,
        port=app_port,
    )
    app_caddy_config_path = app_path / "Caddyfile"
    app_caddy_config_path.write_text(app_caddy_config)

    if (CADDYFILES_PATH / app_name).exists():
        (CADDYFILES_PATH / app_name).unlink()
    (CADDYFILES_PATH / app_name).symlink_to(app_caddy_config_path)

    # -- start app
    subprocess.run(["sudo", "systemctl", "reload", "caddy"], check=True)
    subprocess.run(["sudo", "systemctl", "start", service_name], check=True)


def setup_authorized_keys():
    """command="{entrypoint_path} $SSH_ORIGINAL_COMMAND",no-agent-forwarding,no-user-rc,no-X11-forwarding,no-port-forwarding {pubkey}\n"""


def init():
    """
    apps/
    caddyfiles/
        Caddyfile
        <appname> -> ../apps/<appname>/Caddyfile
    """

    for dir in (APPS_ROOT_PATH, CADDYFILES_PATH):
        dir.mkdir(parents=True, exist_ok=True)

    # -- install deps
    subprocess.run(["poetry", "config", "virtualenvs.in-project", "true"])
    # -- configure poetry

    # -- setup default caddyfile
    with open(CADDYFILES_PATH / "Caddyfile", "w") as f:
        f.write(
            """
* {
    encode zstd gzip
}
"""
        )

    global_caddyfile = Path("/etc/caddy/Caddyfile")

    config = f"""
# --- added by bao
import {CADDYFILES_PATH!s}/*

"""

    if global_caddyfile.exists():
        existent_config = global_caddyfile.read_text()
        if config in existent_config:
            config = ""
        config += existent_config

    with tempfile.NamedTemporaryFile("w", delete=False) as tf:
        tf.write(config)

    subprocess.run(["sudo", "mv", tf.name, str(global_caddyfile)], check=True)
    subprocess.run(["sudo", "chmod", "755", str(global_caddyfile)], check=True)


# --- CLI commands


if __name__ == "__main__":
    print("[Bao]")
    init()
    configure_app("ad_automator_test", Path("/tmp/ad_automator_test"))
