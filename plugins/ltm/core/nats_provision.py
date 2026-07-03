"""Provision a local NATS JetStream server for ``bus=nats`` — binary (default) or Docker.

Opt-in and fail-open: called off the hot path (the detached capture worker); any
failure returns ``False`` and the MemoryBus falls back to the inproc SQLite queue. The
binary path downloads a pinned ``nats-server`` release (checksum-verified) into the
data dir and runs it with JetStream — no Docker prerequisite, mirroring how the plugin
self-provisions its embedding venv. Stdlib only.

Note: this provisions the *server*. The *client* (``nats-py``) must also be importable
by the hook process for ``bus=nats`` to activate; otherwise the bus fails open to inproc.
"""

from __future__ import annotations

import hashlib
import os
import platform
import signal
import socket
import subprocess
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

_RELEASES = "https://github.com/nats-io/nats-server/releases/download"
_CONTAINER = "ltm-nats"


def _host_port(nats_url: str) -> tuple[str, int]:
    parsed = urlparse(nats_url)
    return parsed.hostname or "127.0.0.1", parsed.port or 4222


def _reachable(nats_url: str, timeout: float = 1.0) -> bool:
    host, port = _host_port(nats_url)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _asset_name(version: str) -> str:
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}.get(platform.machine())
    if not system or not arch:
        raise RuntimeError(f"unsupported platform {platform.system()}/{platform.machine()}")
    ext = "zip" if system == "windows" else "tar.gz"
    return f"nats-server-v{version}-{system}-{arch}.{ext}"


def _expected_sha(version: str, asset: str) -> str:
    with urllib.request.urlopen(f"{_RELEASES}/v{version}/SHA256SUMS", timeout=20) as resp:
        for line in resp.read().decode().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == asset:
                return parts[0]
    raise RuntimeError(f"no checksum listed for {asset}")


def _nats_dir(cfg) -> Path:
    return Path(cfg.data_dir) / "nats"


def binary_path(cfg) -> Path:
    exe = "nats-server.exe" if platform.system() == "Windows" else "nats-server"
    return _nats_dir(cfg) / exe


def pid_path(cfg) -> Path:
    return _nats_dir(cfg) / "nats.pid"


def _download_binary(cfg, log) -> Path:
    binary = binary_path(cfg)
    if binary.exists():
        return binary
    version = cfg.nats_version
    asset = _asset_name(version)
    _nats_dir(cfg).mkdir(parents=True, exist_ok=True)
    archive = _nats_dir(cfg) / asset
    log(f"[ltm] downloading nats-server v{version} ({asset})…")
    urllib.request.urlretrieve(f"{_RELEASES}/v{version}/{asset}", archive)
    got = hashlib.sha256(archive.read_bytes()).hexdigest()
    want = _expected_sha(version, asset)
    if got != want:
        archive.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {asset}")
    if asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            member = next(n for n in zf.namelist() if n.endswith(("nats-server", "nats-server.exe")))
            binary.write_bytes(zf.read(member))
    else:
        with tarfile.open(archive) as tf:
            member = next(m for m in tf.getmembers() if m.name.rsplit("/", 1)[-1] == "nats-server")
            binary.write_bytes(tf.extractfile(member).read())
    archive.unlink(missing_ok=True)
    binary.chmod(0o755)
    return binary


def _wait_reachable(cfg, tries: int = 50) -> bool:
    for _ in range(tries):
        if _reachable(cfg.nats_url, 0.2):
            return True
        time.sleep(0.1)
    return _reachable(cfg.nats_url)


def _start_binary(cfg, log) -> bool:
    binary = _download_binary(cfg, log)
    host, port = _host_port(cfg.nats_url)
    store = _nats_dir(cfg) / "store"
    store.mkdir(parents=True, exist_ok=True)
    log(f"[ltm] starting nats-server on {host}:{port} (JetStream)…")
    proc = subprocess.Popen(
        [str(binary), "-js", "-sd", str(store), "-a", host, "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # detached — survives the worker, like the daemon/viewer
    )
    pid_path(cfg).write_text(str(proc.pid))
    return _wait_reachable(cfg)


def _start_docker(cfg, log) -> bool:
    import shutil

    docker = shutil.which("docker")
    if not docker:
        log("[ltm] nats_provision=docker but docker is not installed")
        return False
    _, port = _host_port(cfg.nats_url)
    subprocess.run([docker, "start", _CONTAINER], capture_output=True)  # reuse if it exists
    if not _reachable(cfg.nats_url, 1.0):
        log(f"[ltm] starting nats via docker ({_CONTAINER})…")
        subprocess.run(
            [docker, "run", "-d", "--name", _CONTAINER, "-p", f"{port}:4222", f"nats:{cfg.nats_version}", "--jetstream"],
            capture_output=True,
        )
    return _wait_reachable(cfg)


def ensure_nats(cfg, log=print, *, force: bool = False) -> bool:
    """Ensure a NATS server is reachable for ``bus=nats``. Best-effort, fail-open.

    ``force`` provisions even when ``bus`` isn't ``nats`` (used by ``ltm nats start``).
    """
    if not force and cfg.bus != "nats":
        return False
    if _reachable(cfg.nats_url):
        return True
    try:
        if cfg.nats_provision == "off":
            return False
        if cfg.nats_provision == "docker":
            return _start_docker(cfg, log)
        return _start_binary(cfg, log)
    except Exception as exc:  # download/spawn failure → inproc
        log(f"[ltm] nats provisioning failed: {exc}")
        return False


def status(cfg) -> dict:
    pid = pid_path(cfg).read_text().strip() if pid_path(cfg).exists() else None
    return {
        "url": cfg.nats_url,
        "reachable": _reachable(cfg.nats_url),
        "binary": str(binary_path(cfg)) if binary_path(cfg).exists() else None,
        "pid": pid,
        "provision": cfg.nats_provision,
    }


def stop(cfg) -> bool:
    """Stop a provisioned nats-server (binary PID and/or docker container)."""
    stopped = False
    pidf = pid_path(cfg)
    if pidf.exists():
        try:
            os.kill(int(pidf.read_text().strip()), signal.SIGTERM)
            stopped = True
        except (OSError, ValueError):
            pass
        pidf.unlink(missing_ok=True)
    import shutil

    docker = shutil.which("docker")
    if docker and subprocess.run([docker, "rm", "-f", _CONTAINER], capture_output=True).returncode == 0:
        stopped = True
    return stopped
