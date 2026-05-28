"""Per-project libvirt NAT network management."""
from __future__ import annotations

import ipaddress
import logging
from typing import Optional

import libvirt

from boxer.config import BoxerConfig, get_config

logger = logging.getLogger(__name__)


def _net_xml(net_name: str, bridge: str, ip: str, netmask: str, dhcp_start: str, dhcp_end: str) -> str:
    return f"""<network>
  <name>{net_name}</name>
  <forward mode='nat'/>
  <bridge name='{bridge}' stp='on' delay='0'/>
  <ip address='{ip}' netmask='{netmask}'>
    <dhcp>
      <range start='{dhcp_start}' end='{dhcp_end}'/>
    </dhcp>
  </ip>
</network>"""


def _isolated_net_xml(net_name: str, bridge: str, ip: str, netmask: str, dhcp_start: str, dhcp_end: str) -> str:
    return f"""<network>
  <name>{net_name}</name>
  <bridge name='{bridge}' stp='on' delay='0'/>
  <ip address='{ip}' netmask='{netmask}'>
    <dhcp>
      <range start='{dhcp_start}' end='{dhcp_end}'/>
    </dhcp>
  </ip>
</network>"""


class NetworkManager:
    def __init__(self, conn: libvirt.virConnect, cfg: Optional[BoxerConfig] = None):
        self._conn = conn
        self._cfg = cfg or get_config()

    def net_name(self, project_id: str) -> str:
        return f"boxer-{project_id[2:10]}"  # strip 'p_' prefix, take 8 chars

    def _allocate_subnet(self, project_id: str) -> tuple[str, str, str, str, str]:
        """Return (gateway, netmask, dhcp_start, dhcp_end) for this project."""
        base = ipaddress.ip_network(self._cfg.network_base_cidr)
        prefix = self._cfg.network_prefix_len

        # Stable index derived from project_id to get a consistent subnet
        project_index = int(project_id[2:10], 16) % (2 ** (prefix - base.prefixlen))

        subnets = list(base.subnets(new_prefix=prefix))
        subnet = subnets[project_index % len(subnets)]

        hosts = list(subnet.hosts())
        gateway = str(hosts[0])
        netmask = str(subnet.netmask)
        dhcp_start = str(hosts[1])
        dhcp_end = str(hosts[-1])
        return gateway, netmask, dhcp_start, dhcp_end

    def ensure_project_network(self, project_id: str, isolated: bool = False) -> str:
        net_name = self.net_name(project_id)
        bridge = f"vboxer{project_id[2:8]}"[:15]  # bridge name ≤ 15 chars

        try:
            net = self._conn.networkLookupByName(net_name)
            if net.isActive() == 0:
                net.create()
            return net_name
        except libvirt.libvirtError:
            pass

        gateway, netmask, dhcp_start, dhcp_end = self._allocate_subnet(project_id)
        if isolated:
            xml = _isolated_net_xml(net_name, bridge, gateway, netmask, dhcp_start, dhcp_end)
        else:
            xml = _net_xml(net_name, bridge, gateway, netmask, dhcp_start, dhcp_end)

        net = self._conn.networkDefineXML(xml)
        net.setAutostart(1)
        net.create()
        logger.info("Created network %s (%s/%s) for project %s", net_name, gateway, netmask, project_id)
        return net_name

    def teardown_project_network(self, project_id: str) -> None:
        net_name = self.net_name(project_id)
        try:
            net = self._conn.networkLookupByName(net_name)
            if net.isActive():
                net.destroy()
            net.undefine()
            logger.info("Removed network %s", net_name)
        except libvirt.libvirtError as exc:
            logger.debug("teardown_project_network: %s", exc)

    def has_active_vms(self, project_id: str) -> bool:
        """Return True if any active libvirt domain is using this project's network."""
        net_name = self.net_name(project_id)
        try:
            net = self._conn.networkLookupByName(net_name)
            # libvirt doesn't expose active domain count directly; use DHCPLeases as proxy
            leases = net.DHCPLeases()
            return len(leases) > 0
        except libvirt.libvirtError:
            return False
