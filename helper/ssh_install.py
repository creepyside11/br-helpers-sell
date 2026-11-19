"""Install an authenticated worker on a VPS via pinned SSH."""
import base64
import hashlib
import hmac
import io
import ipaddress
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}\Z")
USERNAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_-]{0,31}\Z")


def validate_ssh(host, username, fingerprint, password):
    address = ipaddress.ip_address(host.strip())
    if not address.is_global:
        raise ValueError("Требуется публичный IP VPS.")
    if not USERNAME.fullmatch(username.strip()) or not FINGERPRINT.fullmatch(fingerprint.strip()):
        raise ValueError("Проверь SSH-логин и SHA256 отпечаток сервера.")
    if not 1 <= len(password) <= 256 or any(ch in password for ch in ("\x00", "\r", "\n")):
        raise ValueError("Проверь пароль SSH.")
    return str(address), username.strip(), fingerprint.strip(), password


def _known_host(host, fingerprint):
    scan = subprocess.run(
        ["ssh-keyscan", "-T", "6", "-t", "ed25519,ecdsa,rsa", host],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=16, check=False,
    )
    for line in scan.stdout.decode("ascii", errors="ignore").splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[0] != host:
            continue
        try:
            key = base64.b64decode(fields[2], validate=True)
        except ValueError:
            continue
        actual = "SHA256:" + base64.b64encode(hashlib.sha256(key).digest()).decode().rstrip("=")
        if hmac.compare_digest(actual, fingerprint):
            return line + "\n", fields[1]
    raise RuntimeError("SSH-ключ не совпал с проверенным отпечатком.")


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


def install_worker(host, username, fingerprint, password, url, token, catalog=""):
    known, key_type = _known_host(host, fingerprint)
    if key_type == "ssh-rsa":
        key_type = "rsa-sha2-512,rsa-sha2-256"
    with tempfile.TemporaryDirectory(prefix="helper-ssh-") as staging:
        stage = Path(staging)
        known_path = stage / "known_hosts"
        known_path.write_text(known)
        askpass = stage / "askpass"
        askpass.write_text('#!/bin/sh\nprintf "%s\\n" "$RAK_HELPER_SSH_PASSWORD"\n')
        askpass.chmod(0o700)
        options = {
            "UserKnownHostsFile": str(known_path), "GlobalKnownHostsFile": "/dev/null",
            "StrictHostKeyChecking": "yes", "HostKeyAlgorithms": key_type,
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
            stderr=subprocess.DEVNULL, env=env, timeout=240, start_new_session=True,
        )
        if result.returncode:
            raise RuntimeError("Установка не завершилась. Проверь SSH, sudo и systemd на VPS.")
