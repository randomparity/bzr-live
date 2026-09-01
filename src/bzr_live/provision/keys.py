from __future__ import annotations

import os
import stat
from pathlib import Path

from .adapters import ProvisionError

_ACTOR_DIR = "actor-keys"
_ADMIN_FILE = "admin.key"


def _require_private_dir(path: Path) -> None:
    details = os.lstat(path)
    if not stat.S_ISDIR(details.st_mode):
        raise ProvisionError(f"{path} is not a directory")
    if stat.S_IMODE(details.st_mode) != 0o700:
        raise ProvisionError(f"{path} must have mode 0700; run: chmod 700 {path}")
    if details.st_uid != os.getuid():
        raise ProvisionError(f"{path} must be owned by the current user")


def _ensure_private_dir(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    except FileExistsError:
        pass
    _require_private_dir(path)


class KeyStore:
    """Owner-only key files: <root>/admin.key and <root>/actor-keys/<name>.key.

    The admin key lives outside actor-keys/ because "admin" is a legal actor slug
    (spec: Actor API keys). Ordinary local fixture hygiene only — 0700/0600 modes,
    no symlinks, owner check (issue #4 boundary).
    """

    def __init__(self, state_root: str | Path) -> None:
        self._root = Path(state_root)
        self._root.parent.mkdir(parents=True, exist_ok=True)
        _ensure_private_dir(self._root)
        _ensure_private_dir(self._root / _ACTOR_DIR)

    def _read(self, path: Path) -> str | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProvisionError(f"cannot open key file {path}: {exc.strerror}") from None
        try:
            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode):
                raise ProvisionError(f"key file {path} is not a regular file")
            if stat.S_IMODE(details.st_mode) != 0o600:
                raise ProvisionError(f"key file {path} must have mode 0600")
            if details.st_uid != os.getuid():
                raise ProvisionError(f"key file {path} must be owned by the current user")
            content = os.read(fd, 4096).decode("utf-8").strip()
        finally:
            os.close(fd)
        if not content:
            raise ProvisionError(f"key file {path} is empty; remove it and rerun")
        return content

    def _write(self, path: Path, key: str) -> None:
        tmp = path.parent / f".tmp-{path.name}"
        tmp.unlink(missing_ok=True)  # a stale tmp from a killed run must not block us
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, (key + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.rename(tmp, path)

    def admin_key(self) -> str | None:
        return self._read(self._root / _ADMIN_FILE)

    def store_admin_key(self, key: str) -> None:
        self._write(self._root / _ADMIN_FILE, key)

    def admin_key_path(self) -> Path:
        return self._root / _ADMIN_FILE

    def actor_key(self, name: str) -> str | None:
        return self._read(self._root / _ACTOR_DIR / f"{name}.key")

    def store_actor_key(self, name: str, key: str) -> None:
        self._write(self._root / _ACTOR_DIR / f"{name}.key", key)
