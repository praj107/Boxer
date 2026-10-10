from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class VMRecord:
    id: str
    libvirt_name: str
    project_id: str
    display_name: str
    state: str  # pending|running|stopped|error|deleting
    owner_user: str
    template: str
    cpu: int
    ram_mb: int
    disk_gb: int
    headless: bool
    created_at: datetime
    last_touched: datetime
    lease_until: datetime
    ip_address: Optional[str] = None
    tags: dict[str, str] = field(default_factory=dict)
    origin: str = "boxer"  # 'boxer' | 'imported' | 'adopted'
    artifact_type: str = "cloud-image"  # 'cloud-image' | 'iso'
    install_state: Optional[str] = None  # None for cloud-images; 'installing'|'installed'|'failed' for ISO VMs
    kind: str = "vm"  # 'vm' (libvirt domain) | 'android' (host-direct emulator device)


@dataclass
class QueueEntry:
    id: str
    project_id: str
    request_json: str
    priority: int
    status: str  # pending|processing|done|expired
    reason: str
    created_at: datetime


@dataclass
class Event:
    id: str
    project_id: Optional[str]
    vm_id: Optional[str]
    level: str  # INFO|WARN|ERROR
    message: str
    created_at: datetime


@dataclass
class CleanupAction:
    vm_id: str
    project_id: str
    stage: str  # graceful_requested
    graceful_requested_at: datetime
    force_after: datetime
    last_error: str = ""


@dataclass
class ProjectNotification:
    id: str
    project_id: str
    vm_id: Optional[str]
    level: str
    message: str
    details: dict[str, object]
    status: str
    created_at: datetime
    delivered_at: Optional[datetime] = None


@dataclass
class CallerIdentity:
    project_id: str
    user: str
    is_admin: bool = False
