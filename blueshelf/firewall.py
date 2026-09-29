"""The Windows Firewall rule that lets a headset reach the page.

Binding a server to every network interface is not enough: Windows still drops the connection
until a rule allows it in, which looks exactly like the server being broken. The rule is limited
to the local subnet, so the page is never reachable from beyond the home network.
"""
from __future__ import annotations

import ctypes
import os
import time

from blueshelf import paths

RULE_PREFIX = "BlueShelf headset page"


def rule_name(port: int) -> str:
    return f"{RULE_PREFIX} ({port})"


def supported() -> bool:
    return os.name == "nt"


def rule_exists(port: int) -> bool:
    if not supported():
        return True
    try:
        result = paths.run_tool(["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule_name(port)}"],
                                timeout=20)
    except Exception:
        return False
    return result.returncode == 0 and b"LocalPort" in (result.stdout or b"")


def network_is_public() -> bool:
    """True when Windows treats the current network as Public, where inbound rules are strictest."""
    if not supported():
        return False
    try:
        result = paths.run_tool(["powershell", "-NoProfile", "-Command",
                                 "(Get-NetConnectionProfile).NetworkCategory"], timeout=25)
    except Exception:
        return False
    return b"Public" in (result.stdout or b"")


def request_rule(port: int) -> bool:
    """Raise the UAC prompt that adds the rule. True when the prompt was accepted and started.

    The rule appears a moment later, so callers poll rule_exists() rather than trusting this.
    """
    if not supported():
        return True
    name = rule_name(port)
    command = (f'/c netsh advfirewall firewall delete rule name="{name}" >nul 2>&1'
               f' & netsh advfirewall firewall add rule name="{name}" dir=in action=allow'
               f' protocol=TCP localport={port} profile=any remoteip=LocalSubnet')
    try:
        started = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", command, None, 0)
    except Exception:
        return False
    return started > 32  # 32 or less means it never started, usually because UAC was declined


def add_rule(port: int, wait: float = 12.0) -> bool:
    """Add the rule and wait for it to show up. Used outside the GUI."""
    if not request_rule(port):
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if rule_exists(port):
            return True
        time.sleep(0.5)
    return rule_exists(port)


def remove_rule(port: int) -> bool:
    if not supported():
        return True
    command = f'/c netsh advfirewall firewall delete rule name="{rule_name(port)}"'
    try:
        return ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", command, None, 0) > 32
    except Exception:
        return False


# --------------------------------------------------------------------------- DLNA

DLNA_PREFIX = "BlueShelf DLNA"


def dlna_rule_names(port: int) -> tuple[str, str]:
    return f"{DLNA_PREFIX} ({port})", f"{DLNA_PREFIX} discovery"


def _rule_present(name: str) -> bool:
    try:
        result = paths.run_tool(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"], timeout=20)
    except Exception:
        return False
    return result.returncode == 0 and b"LocalPort" in (result.stdout or b"")


def dlna_rules_exist(port: int) -> bool:
    """Both halves: TCP for the videos themselves, UDP 1900 for being found at all."""
    if not supported():
        return True
    return all(_rule_present(name) for name in dlna_rule_names(port))


def request_dlna_rules(port: int) -> bool:
    """One permission prompt that adds both DLNA rules, limited to the local network."""
    if not supported():
        return True
    media, discovery = dlna_rule_names(port)
    command = (f'/c netsh advfirewall firewall delete rule name="{media}" >nul 2>&1'
               f' & netsh advfirewall firewall delete rule name="{discovery}" >nul 2>&1'
               f' & netsh advfirewall firewall add rule name="{media}" dir=in action=allow'
               f' protocol=TCP localport={port} profile=any remoteip=LocalSubnet'
               f' & netsh advfirewall firewall add rule name="{discovery}" dir=in action=allow'
               f' protocol=UDP localport=1900 profile=any remoteip=LocalSubnet')
    try:
        started = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", command, None, 0)
    except Exception:
        return False
    return started > 32
