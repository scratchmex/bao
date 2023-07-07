SYSTEMCTL_RESTART_INTERVAL = 3


def get_systemctl_config(gunicorn_cmd: str, working_directory: str, description: str):
    return f"""
[Unit]
Description={description}

[Service]
After=network.target
Restart=always
RestartSec={SYSTEMCTL_RESTART_INTERVAL}
WorkingDirectory={working_directory}
ExecStart={gunicorn_cmd}

[Install]
WantedBy=multi-user.target
""".strip()


def init():
    pass


# --- CLI commands


if __name__ == "__main__":
    print("[Bao]")
