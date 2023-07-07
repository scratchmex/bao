#!/usr/bin/env python3.11
import argparse
import io
import sys

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 required")

import shutil
import subprocess
import tempfile
import tomllib

from pathlib import Path
from dataclasses import dataclass

ROOT_PATH = Path("~").resolve()
APPS_ROOT_PATH = ROOT_PATH / "apps"
CADDYFILES_PATH = ROOT_PATH / "caddyfiles"
SYSTEMDFILES_PATH = ROOT_PATH / "systemdfiles"
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
WantedBy=default.target
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


def add_app(app_name: str, tmp_path: Path):
    """
    apps/
        <appname>/
            root_src/ (source code)
            git/ (git bare repo)
            systemd.service
            Caddyfile

    """
    if not (tmp_path / "bao.toml").exists():
        print("bao.toml not detected")
        sys.exit()

    if not (tmp_path / "pyproject.toml").exists():
        print("pyproject.toml not detected")
        sys.exit(1)

    # -- parse project config
    with open(tmp_path / "bao.toml") as f:
        bao_config = BaoConfig(**tomllib.load(f))

    app_config = bao_config.apps.get(app_name)
    if not app_config:
        print(f"{app_name} not found in bao config")
        sys.exit(1)

    if not (tmp_path / app_config.procfile).exists():
        print(f"{app_config.procfile} not detected")
        sys.exit(1)

    with open(tmp_path / app_config.procfile) as f:
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
    app_service_path = app_path / "systemd.service"
    with open(app_service_path, "w") as f:
        f.write(systemctl_config)

    app_service_name = f"{app_name}.service"
    app_service_symlink_path = SYSTEMDFILES_PATH / app_service_name
    if app_service_symlink_path.is_symlink():
        app_service_symlink_path.unlink()
    app_service_symlink_path.symlink_to(app_service_path)

    subprocess.run(["systemctl", "--user", "enable", app_service_name], check=True)

    # -- configure caddy
    app_caddy_config = get_app_caddyfile_config(
        domain=app_config.domain,
        app_root_src_path=app_root_src_path,
        port=app_port,
    )
    app_caddy_config_path = app_path / "Caddyfile"
    app_caddy_config_path.write_text(app_caddy_config)

    if (CADDYFILES_PATH / app_name).is_symlink():
        (CADDYFILES_PATH / app_name).unlink()
    (CADDYFILES_PATH / app_name).symlink_to(app_caddy_config_path)

    # -- start app
    subprocess.run(["sudo", "systemctl", "reload", "caddy"], check=True)
    subprocess.run(["systemctl", "--user", "start", app_service_name], check=True)


def remove_app(app_name: str):
    app_path = APPS_ROOT_PATH / app_name

    app_service_name = f"{app_name}.service"
    subprocess.run(["systemctl", "--user", "stop", app_service_name], check=True)
    subprocess.run(["systemctl", "--user", "disable", app_service_name], check=True)
    subprocess.run(["sudo", "systemctl", "reload", "caddy"], check=True)

    shutil.rmtree(app_path)

    files_to_delete = (
        SYSTEMDFILES_PATH / f"{app_name}.service",
        CADDYFILES_PATH / app_name,
    )
    for file in files_to_delete:
        if file.is_file():
            file.unlink()


def cmd_del(args: argparse.Namespace):
    app_name = args.app_name
    remove_app(app_name)


def init_systemctl():
    # init user systemd on boot
    # ref: https://wiki.archlinux.org/title/systemd/User#Automatic_start-up_of_systemd_user_instances
    subprocess.run(["sudo", "loginctl", "enable-linger", "bao"], check=True)


def init_caddy():
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

    if global_caddyfile.is_file():
        existent_config = global_caddyfile.read_text()
        if config in existent_config:
            config = ""
        config += existent_config

    with tempfile.NamedTemporaryFile("w", delete=False) as tf:
        tf.write(config)

    subprocess.run(["sudo", "mv", tf.name, str(global_caddyfile)], check=True)
    subprocess.run(["sudo", "chmod", "755", str(global_caddyfile)], check=True)

    # -- configure sudoers
    with tempfile.NamedTemporaryFile("w", delete=False) as tf:
        tf.write("bao ALL=(root) NOPASSWD: /usr/bin/systemctl reload caddy\n")

    subprocess.run(["sudo", "mv", tf.name, "/etc/sudoers.d/bao-caddy"], check=True)
    subprocess.run(
        ["sudo", "chown", "root:root", "/etc/sudoers.d/bao-caddy"], check=True
    )


AUTHORIZED_KEYS_TEMPLATE = """command="{entrypoint_path} $SSH_ORIGINAL_COMMAND",no-agent-forwarding,no-user-rc,no-X11-forwarding,no-port-forwarding {pubkey}\n"""


def init_ssh_access():
    baoscript = Path(__file__).resolve()
    authorized_keys_path = Path("~/.ssh/authorized_keys")
    authorized_keys = authorized_keys_path.read_text()

    new_authorized_keys = io.StringIO()
    for line in authorized_keys.splitlines():
        if not line.startswith("ssh-"):
            new_authorized_keys.write(line)
            continue

        new_authorized_keys.write(
            AUTHORIZED_KEYS_TEMPLATE.format(
                entrypoint_path=str(baoscript), pubkey=line.strip()
            )
        )

    with open(authorized_keys_path, "w") as f:
        f.write(new_authorized_keys.getvalue())


def init():
    """
    apps/
    caddyfiles/
        Caddyfile
        <appname> -> ../apps/<appname>/Caddyfile
    systemdfiles/
        <appname>.service -> ../apps/<appname>/systemd.service
    """
    subprocess.run(["sudo", "echo", "sudo access ok"], check=True)

    ROOT_PATH = Path("/home/bao")
    for dir in (
        ROOT_PATH / "apps",
        ROOT_PATH / "caddyfiles",
        ROOT_PATH / ".config/systemd/user",
    ):
        dir.mkdir(parents=True, exist_ok=True)

    systemdfiles_path = ROOT_PATH / "systemdfiles"
    if systemdfiles_path.is_symlink():
        systemdfiles_path.unlink()
    (ROOT_PATH / "systemdfiles").symlink_to(ROOT_PATH / ".config/systemd/user")

    # -- install deps
    # -- configure poetry
    subprocess.run(["poetry", "config", "virtualenvs.in-project", "true"])

    init_systemctl()

    init_caddy()

    init_ssh_access()

    subprocess.run(["sudo", "chown", "-R", "bao:bao", str(ROOT_PATH)], check=True)


# --- CLI commands


if __name__ == "__main__":
    parser = argparse.ArgumentParser("bao", description="PaaS for Python")
    subparser = parser.add_subparsers()

    init_parser = subparser.add_parser("init", description="init bao")
    init_parser.set_defaults(handle=init)

    del_parser = subparser.add_parser("del", description="delete an app")
    del_parser.add_argument("app_name")
    del_parser.set_defaults(handle=cmd_del)

    print("[Bao]")

    args = parser.parse_args(sys.argv[1:] or ["--help"])

    # TODO: add validation that if command is different than init, it should be bao user

    args.handle()

    # init()
    # configure_app("ad_automator_test", Path("/tmp/ad_automator_test"))
