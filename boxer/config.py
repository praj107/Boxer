from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

_CONFIG_ENV = "BOXER_CONFIG_PATH"
_IMAGES_ENV = "BOXER_IMAGES_PATH"
_DEFAULT_CONFIG_PATH = Path("/etc/boxer/boxer.yaml")
_DEFAULT_IMAGES_PATH = Path("/etc/boxer/images.yaml")
_FALLBACK_CONFIG_PATH = Path(__file__).parent.parent / "config" / "boxer.yaml"
_FALLBACK_IMAGES_PATH = Path(__file__).parent.parent / "config" / "images.yaml"


class ConfigLoadError(RuntimeError):
    """Raised when Boxer configuration exists but cannot be loaded."""


def _configured_path(env_var: str) -> Optional[Path]:
    import os

    raw = os.environ.get(env_var)
    return Path(raw).expanduser() if raw else None


def _load_yaml(primary: Path, fallback: Path, *, env_var: str, label: str) -> dict[str, Any]:
    override = _configured_path(env_var)
    paths = [override] if override is not None else [primary, fallback]

    for path in paths:
        if path is None:
            continue
        try:
            if path.exists():
                with open(path) as f:
                    return yaml.safe_load(f) or {}
        except PermissionError as exc:
            raise ConfigLoadError(
                f"Cannot read Boxer {label} file at {path}. "
                "Run setup again or repair permissions so the Boxer client user can read it."
            ) from exc

    if override is not None:
        raise ConfigLoadError(f"{env_var} points to missing Boxer {label} file: {override}")
    return {}


class BoxerConfig:
    def __init__(self, data: dict[str, Any]):
        self._d = data

    # Paths
    @property
    def state_dir(self) -> Path:
        return Path(self._d.get("state_dir", "/var/lib/boxer"))

    @property
    def socket_path(self) -> Path:
        return Path(self._d.get("socket_path", "/run/boxer/boxer.sock"))

    @property
    def notify_socket_path(self) -> Path:
        return Path(self._d.get("notify_socket_path", "/run/boxer/boxer-notify.sock"))

    @property
    def db_path(self) -> Path:
        return self.state_dir / "boxer.db"

    @property
    def images_dir(self) -> Path:
        return self.state_dir / "images"

    @property
    def isos_dir(self) -> Path:
        return self.state_dir / "isos"

    @property
    def keyrings_dir(self) -> Path:
        """Directory of pinned PGP keyrings used to verify checksum manifests."""
        return Path(self._d.get("keyrings_dir", "/etc/boxer/keyrings"))

    @property
    def projects_dir(self) -> Path:
        return self.state_dir / "projects"

    # Resource caps
    @property
    def host_reserved_ram_gib(self) -> float:
        return float(self._d.get("host_reserved_ram_gib", 4.0))

    @property
    def max_total_vcpus(self) -> Optional[int]:
        return self._d.get("max_total_vcpus")  # None = auto (host_threads - 2)

    @property
    def max_concurrent_installs(self) -> int:
        return int(self._d.get("max_concurrent_installs", 1))

    @property
    def max_running_per_project(self) -> int:
        return int(self._d.get("max_running_per_project", 2))

    @property
    def max_disk_per_project_gib(self) -> int:
        return int(self._d.get("max_disk_per_project_gib", 80))

    # Lease defaults
    @property
    def default_ttl_minutes(self) -> int:
        return int(self._d.get("default_ttl_minutes", 60))

    @property
    def stale_warn_grace_minutes(self) -> int:
        return int(self._d.get("stale_warn_grace_minutes", 15))

    @property
    def stale_delete_grace_hours(self) -> int:
        return int(self._d.get("stale_delete_grace_hours", 24))

    # SSH
    @property
    def boxer_ssh_pubkey(self) -> Optional[str]:
        return self._d.get("boxer_ssh_pubkey")

    @property
    def boxer_ssh_privkey_path(self) -> Optional[Path]:
        p = self._d.get("boxer_ssh_privkey_path")
        return Path(p) if p else None

    # Libvirt
    @property
    def libvirt_uri(self) -> str:
        return self._d.get("libvirt_uri", "qemu:///system")

    # Network
    @property
    def network_base_cidr(self) -> str:
        return self._d.get("network_base_cidr", "10.200.0.0/16")

    @property
    def network_prefix_len(self) -> int:
        return int(self._d.get("network_prefix_len", 24))

    # Admin group
    @property
    def admin_group(self) -> str:
        return self._d.get("admin_group", "boxer-admin")

    # Local ISO support
    @property
    def local_iso_dir(self) -> Optional[Path]:
        """Directory from which local ISO files may be read via iso_path in box_request_installer.

        None (default) permits any accessible absolute ISO path, which is suitable
        for a single-developer workstation. Set this to a directory that contains
        custom-built ISOs to restrict iso_path to that subtree on shared hosts.
        """
        p = self._d.get("local_iso_dir")
        return Path(p).expanduser() if p else None

    @property
    def qemu_group(self) -> str:
        """Host group used by QEMU processes for VM disk/ISO access."""
        return self._d.get("qemu_group", "kvm")


class ImageCatalog:
    def __init__(self, data: dict[str, Any]):
        self._images: dict[str, dict[str, Any]] = data.get("images", {})

    def get(self, name: str) -> Optional[dict[str, Any]]:
        return self._images.get(name)

    def list_names(self) -> list[str]:
        return list(self._images.keys())


_config: Optional[BoxerConfig] = None
_catalog: Optional[ImageCatalog] = None


def get_config() -> BoxerConfig:
    global _config
    if _config is None:
        _config = BoxerConfig(
            _load_yaml(_DEFAULT_CONFIG_PATH, _FALLBACK_CONFIG_PATH, env_var=_CONFIG_ENV, label="config")
        )
    return _config


def get_catalog() -> ImageCatalog:
    global _catalog
    if _catalog is None:
        _catalog = ImageCatalog(
            _load_yaml(_DEFAULT_IMAGES_PATH, _FALLBACK_IMAGES_PATH, env_var=_IMAGES_ENV, label="image catalog")
        )
    return _catalog


def reload():
    global _config, _catalog
    _config = None
    _catalog = None
