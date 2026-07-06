"""Self-provision a private fastembed venv under the plugin's data dir.

Makes semantic recall seamless for anyone: on first use with ``embedding=fastembed``
the plugin builds its own venv and installs the embedding deps — no manual pip, no
interpreter juggling. The venv lives under CLAUDE_PLUGIN_DATA (survives plugin
updates), not the code folder.

Prefers ``uv`` when present: it is far faster and can fetch a suitable CPython
itself, so provisioning works even when the only system Python is unsupported
(e.g. 3.14). Falls back to ``venv`` + ``pip`` with a system Python 3.10-3.13.
Falls back to the hash embedder if neither can produce a usable environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# uv fetches this itself if absent; the pip fallback needs it already installed.
_UV_PYTHON = "3.12"
# onnxruntime (fastembed's dep) has the most reliable wheels on 3.12/3.11.
_MIN, _MAX = (3, 10), (3, 13)
_CANDIDATES = ["python3.12", "python3.11", "python3.13", "python3.10", "python3", "python"]
_REQ_FILE = Path(__file__).resolve().parent.parent / "requirements.txt"


def requirements() -> list[str]:
    try:
        lines = _REQ_FILE.read_text(encoding="utf-8").splitlines()
        reqs = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
        return reqs or ["fastembed"]
    except OSError:
        return ["fastembed"]


def _pyver(exe: str) -> tuple[int, int] | None:
    try:
        out = subprocess.run(
            [exe, "-c", "import sys;print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        major, minor = out.stdout.split()
        return (int(major), int(minor))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def find_base_python() -> str | None:
    seen: set[str] = set()
    for name in _CANDIDATES:
        exe = shutil.which(name)
        if not exe:
            continue
        real = os.path.realpath(exe)
        if real in seen:
            continue
        seen.add(real)
        version = _pyver(exe)
        if version and _MIN <= version <= _MAX:
            return exe
    return None


def venv_dir(data_dir: str | os.PathLike) -> Path:
    return Path(data_dir) / "venv"


def venv_python(data_dir: str | os.PathLike) -> Path:
    base = venv_dir(data_dir)
    return base / "Scripts" / "python.exe" if os.name == "nt" else base / "bin" / "python"


def _marker(data_dir: str | os.PathLike) -> Path:
    return Path(data_dir) / ".fastembed-ready"


def is_provisioned(data_dir: str | os.PathLike) -> bool:
    return _marker(data_dir).exists() and venv_python(data_dir).exists()


def _provision_with_uv(uv: str, venv: Path, py: str, log) -> None:
    log(f"[engram] provisioning embedding env via uv (python {_UV_PYTHON})…")
    subprocess.run([uv, "venv", "--python", _UV_PYTHON, str(venv)], check=True)
    subprocess.run([uv, "pip", "install", "--python", py, *requirements()], check=True)


def _provision_with_pip(venv: Path, py: str, log) -> None:
    base = find_base_python()
    if not base:
        raise RuntimeError(
            "no uv and no suitable Python 3.10-3.13 found — install uv "
            "(https://astral.sh/uv) or a Python 3.12, or keep embedding=hash"
        )
    log(f"[engram] provisioning embedding env with {base} + pip (one-time, ~1 min)…")
    subprocess.run([base, "-m", "venv", str(venv)], check=True)
    subprocess.run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"], check=True)
    subprocess.run([py, "-m", "pip", "install", "-q", *requirements()], check=True)


def provision(data_dir: str | os.PathLike, log=print) -> bool:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    if is_provisioned(data_dir):
        log("[engram] embedding env already provisioned")
        return True
    lock = data_dir / ".provision.lock"
    try:
        os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        log("[engram] provisioning already in progress")
        return False
    try:
        venv = venv_dir(data_dir)
        py = str(venv_python(data_dir))
        uv = shutil.which("uv")
        if uv:
            _provision_with_uv(uv, venv, py, log)
        else:
            _provision_with_pip(venv, py, log)
        _marker(data_dir).write_text("ok")
        log("[engram] embedding env ready — semantic recall active next session")
        return True
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        log(f"[engram] provisioning failed: {exc}")
        return False
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _has_nats_py(py_exe: str) -> bool:
    try:
        subprocess.run(
            [py_exe, "-c", "import nats"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_nats_py_in_venv(data_dir: str | os.PathLike, log=print) -> bool:
    data_dir = Path(data_dir)
    marker = data_dir / ".nats-py-ready"
    venv = venv_dir(data_dir)
    py = str(venv_python(data_dir))
    if not venv.exists() or not Path(py).exists():
        return False
    if marker.exists() and _has_nats_py(py):
        return True
    try:
        log("[engram] installing nats-py into managed venv…")
        # A uv-created venv has no pip, so install via `uv pip --python`; fall back to
        # the venv's own pip for pip-created venvs.
        uv = shutil.which("uv")
        if uv:
            subprocess.run([uv, "pip", "install", "--python", py, "nats-py"], check=True, timeout=60)
        else:
            subprocess.run([py, "-m", "pip", "install", "-q", "nats-py"], check=True, timeout=60)
        marker.write_text("ok")
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"[engram] nats-py install failed (NATS will degrade to inproc): {exc}")
        return False
