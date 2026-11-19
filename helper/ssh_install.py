"""Install a VPS worker using a persistent OpenSSH known_hosts file."""
import io
import ipaddress
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import tarfile
import tempfile

from .common import data_directory

ROOT = Path(__file__).resolve().parents[1]
USERNAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_-]{0,31}\Z")


def validate_ssh_host(host):
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError:
        raise ValueError("Введи публичный IP VPS без порта.") from None
    if not address.is_global:
        raise ValueError("Требуется публичный IP VPS.")
    return str(address)


def validate_ssh_username(username):
    if not USERNAME.fullmatch(username.strip()):
        raise ValueError("Проверь SSH-логин.")
    return username.strip()


def validate_ssh_password(password):
    if not 1 <= len(password) <= 256 or any(ch in password for ch in ("\x00", "\r", "\n")):
        raise ValueError("Проверь пароль SSH.")
    return password


def _archive(url, token, catalog):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
        for name in ("__init__.py", "common.py", "game.py", "worker.py"):
            bundle.add(ROOT / "helper" / name, arcname="helper/" + name, recursive=False)
        for path in sorted((ROOT / "runtime").rglob("*.py")):
            bundle.add(path, arcname=str(path.relative_to(ROOT)), recursive=False)
        bundle.add(ROOT / "deploy/install_worker.sh", arcname="deploy/install_worker.sh", recursive=False)
        config = f"WORKER_URL={url}\nWORKER_TOKEN={token}\n"
        if catalog:
            config += "BR_SERVERS_URL=" + shlex.quote(catalog) + "\n"
        data = config.encode()
        entry = tarfile.TarInfo("worker.env")
        entry.mode = 0o600
        entry.size = len(data)
        bundle.addfile(entry, io.BytesIO(data))
    return archive.getvalue()


def install_worker(host, username, password, url, token, catalog=""):
    ssh_dir = data_directory() / "ssh"
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    known_path = ssh_dir / "known_hosts"
    fd = os.open(known_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("Хранилище SSH-ключей должно быть обычным файлом.")
    finally:
        os.close(fd)
    # First contact adds the key; subsequent installs must match it.
    with tempfile.TemporaryDirectory(prefix="helper-ssh-") as staging:
        stage = Path(staging)
        askpass = stage / "askpass"
        askpass.write_text('#!/bin/sh\nprintf "%s\\n" "$RAK_HELPER_SSH_PASSWORD"\n')
        askpass.chmod(0o700)
        options = {
            "UserKnownHostsFile": str(known_path), "GlobalKnownHostsFile": "/dev/null",
            "StrictHostKeyChecking": "accept-new", "UpdateHostKeys": "no", "HashKnownHosts": "yes",
            "ConnectTimeout": "10", "NumberOfPasswordPrompts": "1",
            "PreferredAuthentications": "password", "PubkeyAuthentication": "no",
            "ClearAllForwardings": "yes", "ServerAliveInterval": "15", "ServerAliveCountMax": "2",
        }
        env = {"PATH": os.defpath, "SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force",
               "DISPLAY": "helper:0", "RAK_HELPER_SSH_PASSWORD": password}
        script = """set -eu
umask 077
d=$(mktemp -d)
trap 'rm -rf "$d"' EXIT
tar -xzf - -C "$d"
cd "$d"
sh deploy/install_worker.sh
"""
        command = ("" if username == "root" else "sudo -n ") + "sh -c " + shlex.quote(script)
        args = ["ssh", "-F", "/dev/null", "-T", "-p", "22"]
        for key, value in options.items():
            args += ["-o", f"{key}={value}"]
        result = subprocess.run(
            args + ["-l", username, host, command],
            input=_archive(url, token, catalog), stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, env=env, timeout=240, start_new_session=True,
        )
        if result.returncode:
            if b"REMOTE HOST IDENTIFICATION HAS CHANGED" in result.stderr:
                raise RuntimeError("SSH-ключ VPS изменился. Проверь новый ключ в панели провайдера.")
            raise RuntimeError("Установка не завершилась. Проверь SSH, sudo и systemd на VPS.")
