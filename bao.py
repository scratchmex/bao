#!/usr/bin/env python3.11
import argparse
import io
import logging
import os
import socket
import stat
import sys

if sys.version_info < (3, 11):
    sys.exit("Python 3.11 required")

import shutil
import subprocess
import tempfile
import tomllib

from pathlib import Path
from dataclasses import dataclass


logging.basicConfig(level=logging.DEBUG, format="{levelname} {message}", style="{")

logger = logging.getLogger(__name__)


ROOT_PATH = Path("/home/bao").resolve()
APPS_ROOT_PATH = ROOT_PATH / "apps"
CADDYFILES_PATH = ROOT_PATH / "caddyfiles"
SYSTEMDFILES_PATH = ROOT_PATH / "systemdfiles"
SYSTEMCTL_RESTART_INTERVAL = 3
BAO_BIN_PATH = "/home/bao/.local/bin"


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
        logger.info("web cmd should start with python")
        sys.exit(1)

    if "$PORT" not in web_cmd:
        logger.info("$PORT is not present on web cmd")
        sys.exit(1)

    return Procfile(web_cmd=web_cmd)


def get_app_caddyfile_config(
    domain: str, app_root_src_path: str, port: int, static_path: str
):
    return (
        """
{domain} {
    reverse_proxy localhost:{port}

    handle_path /static/* {
        file_server {
            root {app_root_src_path}/{static_path}
        }
    }
}
""".strip()
        .replace("{domain}", domain)
        .replace("{app_root_src_path}", str(app_root_src_path))
        .replace("{port}", str(port))
        .replace("{static_path}", static_path)
    )


@dataclass
class BaoConfigApp:
    domain: str
    static: str
    procfile: str = "Procfile"


@dataclass
class BaoConfig:
    """bao.toml"""

    apps: dict[str, BaoConfigApp]


def add_app(app_name: str):
    """
    apps/
        <appname>/
            code/ (source code)
            repo/ (git bare repo)
            <appname>.service
            Caddyfile

    """
    pass


def get_free_port():
    """Find a free TCP port (entirely at random)"""

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))  # lgtm [py/bind-socket-all-network-interfaces]
    port = s.getsockname()[1]
    s.close()
    return port


def deploy_app(app_name: str):
    app_path = APPS_ROOT_PATH / app_name
    app_code_path = app_path / "code"

    if not (app_code_path / "bao.toml").exists():
        logger.info("bao.toml not detected")
        sys.exit(1)

    if not (app_code_path / "pyproject.toml").exists():
        logger.info("pyproject.toml not detected")
        sys.exit(1)

    # -- parse project config
    with open(app_code_path / "bao.toml", "rb") as f:
        data = tomllib.load(f)
    bao_config = BaoConfig(apps={s: BaoConfigApp(**d) for s, d in data["apps"].items()})

    app_config = bao_config.apps.get(app_name)
    if not app_config:
        logger.info(f"{app_name} not found in bao config")
        sys.exit(1)

    logger.info(f"config: {app_config}")

    if not (app_code_path / app_config.procfile).exists():
        logger.info(f"{app_config.procfile} not detected")
        sys.exit(1)

    with open(app_code_path / app_config.procfile) as f:
        procfile = parse_procfile(f.read())

    # -- configure poetry
    subprocess.run(
        ["poetry", "install"],
        cwd=app_code_path,
        check=True,
        env={"PATH": f"{BAO_BIN_PATH}:{os.environ['PATH']}"},
    )
    subprocess.run([str(app_code_path / ".venv/bin/python"), "-V"], check=True)
    # TODO: remove check=True and do proper validation and printing

    # -- configure node
    # this is useful for assets generated with vite for example
    if (app_code_path / "package.json").exists():
        subprocess.run(
            ["yarn", "install"],
            cwd=app_code_path,
            check=True,
            env={"PATH": f"{BAO_BIN_PATH}:{os.environ['PATH']}"},
        )

    # -- configure systemctl
    web_cmd = procfile.web_cmd
    app_port = get_free_port()
    # TODO: maybe use the same port when redeploying?
    web_cmd = web_cmd.replace("$PORT", str(app_port))
    logger.info(f"--- web cmd: {web_cmd!r}")

    systemctl_config = get_systemctl_config(
        web_cmd=f"{app_code_path / '.venv/bin'}/{web_cmd}",
        working_directory=app_code_path,
        description=f"{app_name} configured by bao",
    )
    app_service_path = app_path / f"{app_name}.service"
    with open(app_service_path, "w") as f:
        f.write(systemctl_config)

    subprocess.run(["systemctl", "--user", "enable", app_service_path], check=True)

    # -- configure caddy
    app_caddy_config = get_app_caddyfile_config(
        domain=app_config.domain,
        app_root_src_path=app_code_path,
        port=app_port,
        static_path=app_config.static,
    )
    app_caddy_config_path = app_path / "Caddyfile"
    app_caddy_config_path.write_text(app_caddy_config)

    if (CADDYFILES_PATH / app_name).is_symlink():
        (CADDYFILES_PATH / app_name).unlink()
    (CADDYFILES_PATH / app_name).symlink_to(app_caddy_config_path)

    # -- start app
    subprocess.run(["sudo", "systemctl", "reload", "caddy"], check=True)
    logger.info("caddy reloaded")
    subprocess.run(["systemctl", "--user", "restart", app_name], check=True)
    logger.info(f"{app_name} service restarted")


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
        config = existent_config + config

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
    authorized_keys_path = Path("/home/bao/.ssh/authorized_keys")
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
    systemdfiles -> .config/systemd/user
    """
    subprocess.run("sudo echo 'sudo access ok'", shell=True, check=True)

    subprocess.run(["sudo", "chmod", "-R", "o=rwx", str(ROOT_PATH)], check=True)
    for dir in (
        APPS_ROOT_PATH,
        CADDYFILES_PATH,
        ROOT_PATH / ".config/systemd/user",
    ):
        dir.mkdir(parents=True, exist_ok=True)

    systemdfiles_path = ROOT_PATH / "systemdfiles"
    if systemdfiles_path.is_symlink():
        systemdfiles_path.unlink()
    (ROOT_PATH / "systemdfiles").symlink_to(ROOT_PATH / ".config/systemd/user")

    # -- install deps

    init_systemctl()

    init_caddy()

    init_ssh_access()

    subprocess.run(["sudo", "chmod", "-R", "o=rx", str(ROOT_PATH)], check=True)
    subprocess.run(["sudo", "chown", "-R", "bao:bao", str(ROOT_PATH)], check=True)

    # -- configure poetry
    subprocess.run(
        ["sudo", "-iu", "bao", "poetry", "config", "virtualenvs.in-project", "true"],
        check=True,
    )


# --- CLI commands
def cmd_init(args: argparse.Namespace):
    init()


def cmd_del(args: argparse.Namespace):
    app_name = args.app_name
    remove_app(app_name)


def cmd_git_receive_pack(args: argparse.Namespace):
    app_name: str = args.app_name[1:-1]  # remove quotes
    app_path = APPS_ROOT_PATH / app_name
    repo_path = app_path / "repo"

    app_path.mkdir(exist_ok=True)

    if not repo_path.is_dir():
        subprocess.run(
            ["git", "init", "--bare", "--quiet", "repo"],
            cwd=app_path,
            check=True,
        )

    git_hook_path = repo_path / "hooks" / "post-receive"
    if not git_hook_path.is_file():
        with open(git_hook_path, "w") as f:
            f.write(
                f"""#!/usr/bin/bash
set -e; set -o pipefail;
cat | /home/bao/bao.py git-hook {app_name}
"""
            )
        git_hook_path.chmod(git_hook_path.stat().st_mode | stat.S_IEXEC)

    # FIXME: redeploy when unsuccessfully last deployment even though we have the code
    subprocess.run(
        ["git", "shell", "-c", "git receive-pack 'repo'"], cwd=app_path, check=True
    )

    # add_app(app_name)


def cmd_git_hook(args: argparse.Namespace):
    app_name = args.app_name
    app_path = APPS_ROOT_PATH / app_name
    logger.info(f"--- post-receive hook called for {app_name}")

    lines = list(sys.stdin)
    if len(lines) != 1:
        print(f"I don't know what do to with this input: {lines}")

    oldrev, newrev, refname = lines[0].strip().split(" ")

    app_path = APPS_ROOT_PATH / app_name

    subprocess.run(
        ["git", "clone", "repo", "code"],
        cwd=app_path,
        check=False,
    )

    code_path = app_path / "code"
    # FIXME: "fatal: not a git repository: '.'"
    subprocess.run(["ls", "-la"], cwd=code_path)
    subprocess.run(
        ["git", "fetch"],
        cwd=code_path,
        env={"GIT_DIR": str(code_path / ".git")},
    )
    subprocess.run(
        ["git", "reset", "--hard", newrev],
        cwd=code_path,
        env={"GIT_DIR": str(code_path / ".git")},
    )

    deploy_app(app_name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser("bao", description="PaaS for Python")
    subparser = parser.add_subparsers()

    init_parser = subparser.add_parser("init", description="init bao")
    init_parser.set_defaults(handle=cmd_init)

    del_parser = subparser.add_parser("del", description="delete an app")
    del_parser.add_argument("app_name")
    del_parser.set_defaults(handle=cmd_del)

    git_receive_pack_parser = subparser.add_parser(
        "git-receive-pack", description="[internal git] used in push"
    )
    git_receive_pack_parser.add_argument("app_name")
    git_receive_pack_parser.set_defaults(handle=cmd_git_receive_pack)

    git_hook_parser = subparser.add_parser(
        "git-hook", description="[internal git] used in post-receive hook"
    )
    git_hook_parser.add_argument("app_name")
    git_hook_parser.set_defaults(handle=cmd_git_hook)

    # logger.info("[Bao]")
    # logger.info(f"{sys.argv=}")

    args = parser.parse_args(sys.argv[1:] or ["--help"])

    # TODO: add validation that if command is different than init, it should be bao user
    try:
        args.handle(args)
    except:
        logger.exception("ops")

    # init()
    # configure_app("ad_automator_test", Path("/tmp/ad_automator_test"))
