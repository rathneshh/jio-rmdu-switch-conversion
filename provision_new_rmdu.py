#!/usr/bin/env python3
"""
================================================================================
 Comtrend / Jio RMDU (JRDU4311) Zero-Touch Network Provisioning Utility
================================================================================

 Developers:     junkmahn & Antigravity

 Purpose:
   Converts a locked, factory-sealed RMDU into an unmanaged, flat 8-port
   Gigabit Ethernet switch directly over the network.
   No physical disassembly or UART soldering required!

 Features:
   - Interactive, auto-detecting Ethernet adapter selector.
   - Automatic VLAN 960 subinterface creation and teardown.
   - Safe signal handling (cleans up network interfaces on exit/Ctrl+C).
   - Zero-touch HTTP activation of Dropbear SSH.
   - Automated conversion payload (VLANs, Isolation, TR-069, MQTT disabled).
   - Post-conversion flat switch reboot verification.

 Usage:
   sudo ./provision_new_rmdu.py [optional_interface_name]
================================================================================
"""

import sys
import os
import time
import glob
import signal
import socket
import shutil
import subprocess
import urllib.request
import pexpect

# ANSI Color codes for clean, readable CLI UI
C_RESET  = "\033[0m"
C_BOLD   = "\033[1m"
C_RED    = "\033[91m"
C_GREEN  = "\033[92m"
C_YELLOW = "\033[93m"
C_BLUE   = "\033[94m"
C_CYAN   = "\033[96m"

# Target defaults
RMDU_IP        = "192.168.33.1"
RMDU_ROOT_USER = "root"
RMDU_ROOT_PASS = "YG*vd2!C2DL=LYXn3ni@"
VLAN_ID        = 960
HOST_VLAN_IP   = "192.168.33.2/24"

# Global state for signal and exit cleanup
CREATED_VLAN_IFACE = None
SELECTED_PARENT_IFACE = None
DEVICE_SET_UNMANAGED = False

NMCLI_AVAILABLE = shutil.which("nmcli") is not None

def set_device_managed(iface, managed):
    """Tell NetworkManager to start/stop managing this device.

    While a device is 'unmanaged', NetworkManager completely ignores whatever happens to
    its IP addresses/routes — so our raw `ip addr`/`ip link` commands never get observed,
    and NM never spawns one of its auto-generated 'assumed connection' profiles to try to
    represent the external change. Handing the device back with managed=yes makes NM
    re-evaluate it exactly as if the cable had just been plugged in, which lets it cleanly
    reconnect using whatever real saved profile already existed — no guessing which
    profile is 'the right one' required, and nothing to clean up in nmcli afterwards.

    Marking a device unmanaged does NOT tear down its existing DHCP lease/IP; NM simply
    stops tracking further changes to it until it's managed again. No-ops quietly if
    nmcli/NetworkManager isn't present on this system at all (e.g. a systemd-networkd-only
    setup), since in that case NM can't be the one interfering in the first place.
    """
    global DEVICE_SET_UNMANAGED
    if not NMCLI_AVAILABLE:
        return
    state = "no" if not managed else "yes"
    subprocess.run(f"nmcli device set {iface} managed {state}", shell=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    DEVICE_SET_UNMANAGED = not managed

def cleanup_host_network(signum=None, frame=None):
    """Ensure all temporary VLANs and 192.168.33.2 aliases are cleanly removed."""
    global CREATED_VLAN_IFACE, SELECTED_PARENT_IFACE
    if CREATED_VLAN_IFACE:
        subprocess.run(f"ip link delete {CREATED_VLAN_IFACE}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        CREATED_VLAN_IFACE = None
        
    if SELECTED_PARENT_IFACE:
        # Delete the temporary secondary IP alias from the physical interface, if present.
        subprocess.run(f"ip addr del {HOST_VLAN_IP} dev {SELECTED_PARENT_IFACE} 2>/dev/null", shell=True)

        # Hand the device back to NetworkManager. This is the real fix for the
        # "assumed connection with no internet" bug: NM never saw our external changes
        # (the device was marked unmanaged while we worked on it), so it has nothing
        # bogus to clean up or reactivate — it just reconnects normally, like a fresh
        # cable-plug event. NEVER invoke `nmcli con mod`/`nmcli con up` against the
        # user's real connection profile here or anywhere else in this script: doing so
        # is what caused NetworkManager to persist a broken, disabled-IPv4 profile in
        # the first place.
        if DEVICE_SET_UNMANAGED:
            set_device_managed(SELECTED_PARENT_IFACE, True)
            
    if signum is not None:
        print(f"\n{C_RED}[!] Operation cancelled by user. Host network cleaned up.{C_RESET}")
        sys.exit(130)

signal.signal(signal.SIGINT, cleanup_host_network)
signal.signal(signal.SIGTERM, cleanup_host_network)

def read_sysfs(path):
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except Exception:
        return ""

def list_network_interfaces():
    """Scan and list network interfaces with carrier, speed, and hardware info."""
    interfaces = []
    for ifpath in sorted(glob.glob('/sys/class/net/*')):
        name = os.path.basename(ifpath)
        if name == 'lo' or '.' in name or name.startswith('vlan'):
            # Skip loopback and existing VLAN subinterfaces
            continue
        
        is_wireless = os.path.exists(f"{ifpath}/wireless")
        carrier = read_sysfs(f"{ifpath}/carrier") == "1"
        operstate = read_sysfs(f"{ifpath}/operstate")
        speed = read_sysfs(f"{ifpath}/speed")
        mac = read_sysfs(f"{ifpath}/address")
        
        dev_type = "Wi-Fi" if is_wireless else "Ethernet"
        link_str = "LINK UP (Connected)" if carrier else "NO LINK (Disconnected)"
        speed_str = f"{speed} Mbps" if speed and speed != "-1" else ""
        
        interfaces.append({
            "name": name,
            "type": dev_type,
            "carrier": carrier,
            "operstate": operstate,
            "speed": speed_str,
            "mac": mac,
            "link_status": link_str,
            "is_ethernet": not is_wireless
        })
    return interfaces

def select_interface_interactively(cli_arg=None):
    """Present a friendly menu for the user to select the Ethernet adapter."""
    interfaces = list_network_interfaces()
    
    if cli_arg:
        match = next((i for i in interfaces if i["name"] == cli_arg), None)
        if match:
            return match["name"]
        else:
            print(f"{C_RED}[!] Specified interface '{cli_arg}' was not found.{C_RESET}")

    print(f"\n{C_BOLD}{C_CYAN}Available Network Adapters:{C_RESET}")
    print("=" * 76)
    print(f" {'#':<3} {'Interface':<17} {'Type':<10} {'Link Status':<24} {'Speed':<10}")
    print("-" * 76)
    
    recommended_idx = None
    for idx, iface in enumerate(interfaces, 1):
        is_rec = iface["is_ethernet"] and iface["carrier"]
        if is_rec and recommended_idx is None:
            recommended_idx = idx
            
        color = C_GREEN if is_rec else (C_RESET if iface["carrier"] else "\033[90m")
        rec_tag = f" {C_YELLOW}[RECOMMENDED]{C_RESET}" if is_rec else ""
        
        print(f"{color} [{idx}] {iface['name']:<17} {iface['type']:<10} {iface['link_status']:<24} {iface['speed']:<10}{rec_tag}{C_RESET}")
        print(f"     └─ MAC: {iface['mac']}")
    print("=" * 76)

    prompt = f"\nSelect adapter [1-{len(interfaces)}]"
    if recommended_idx:
        prompt += f" (default: {recommended_idx})"
    prompt += ": "

    while True:
        try:
            choice = input(prompt).strip()
            if not choice and recommended_idx:
                return interfaces[recommended_idx - 1]["name"]
            val = int(choice)
            if 1 <= val <= len(interfaces):
                return interfaces[val - 1]["name"]
            print(f"{C_RED}Please enter a number between 1 and {len(interfaces)}.{C_RESET}")
        except ValueError:
            print(f"{C_RED}Invalid input. Please enter a number.{C_RESET}")

def check_ping(ip, count=1, timeout=1):
    res = subprocess.run(f"ping -c {count} -W {timeout} {ip}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return res.returncode == 0

def check_port_open(ip, port, timeout=1.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        s.close()
        return True
    except Exception:
        return False

def setup_vlan_interface(parent_iface):
    global CREATED_VLAN_IFACE
    # Linux kernel enforces IFNAMSIZ (maximum 15 characters for interface names).
    # Modern systemd predictable names like 'enx001122334455' are already 15 chars,
    # so appending '.960' overflows. We use a concise, dedicated name 'vlan960' (7 chars).
    vlan_iface = f"vlan{VLAN_ID}"
    print(f"{C_CYAN}[*] Setting up 802.1Q VLAN {VLAN_ID} subinterface '{vlan_iface}' on {parent_iface}...{C_RESET}")
    
    # Clean up any stale interface from previous aborted runs
    subprocess.run(f"ip link delete {vlan_iface}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Create the VLAN subinterface linked to the selected parent adapter
    subprocess.run(f"ip link add link {parent_iface} name {vlan_iface} type vlan id {VLAN_ID}", shell=True, check=True)
    subprocess.run(f"ip addr replace {HOST_VLAN_IP} dev {vlan_iface}", shell=True, check=True)
    subprocess.run(f"ip link set dev {vlan_iface} up", shell=True, check=True)
    subprocess.run(f"ip route replace {RMDU_IP} dev {vlan_iface} src 192.168.33.2", shell=True, check=False)
    CREATED_VLAN_IFACE = vlan_iface
    print(f"{C_GREEN}[+] Created {vlan_iface} with IP {HOST_VLAN_IP}{C_RESET}")
    return vlan_iface

def teardown_vlan_interface(vlan_iface):
    global CREATED_VLAN_IFACE
    print(f"{C_CYAN}[*] Removing temporary VLAN subinterface {vlan_iface}...{C_RESET}")
    subprocess.run(f"ip link delete {vlan_iface}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    CREATED_VLAN_IFACE = None

def trigger_http_endpoint(endpoint_name):
    url = f"http://{RMDU_IP}/{endpoint_name}"
    print(f"{C_CYAN}[*] Sending activation trigger: {url}{C_RESET}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RMDU-Provisioner/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            return resp.status in (200, 204)
    except urllib.error.HTTPError as e:
        return e.code in (200, 204)
    except Exception as e:
        return False

def execute_remote_ssh_pipeline(commands):
    print(f"{C_CYAN}[*] Opening SSH session to {RMDU_ROOT_USER}@{RMDU_IP}:22...{C_RESET}")
    cmd = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 {RMDU_ROOT_USER}@{RMDU_IP}"
    child = pexpect.spawn(cmd, timeout=10)
    
    idx = child.expect(['[Pp]assword:', pexpect.EOF, pexpect.TIMEOUT])
    if idx != 0:
        raise RuntimeError("Target did not offer an SSH password prompt.")
    
    child.sendline(RMDU_ROOT_PASS)
    child.expect(['# ', pexpect.TIMEOUT])
    print(f"{C_GREEN}[+] Authenticated as root! Applying switch conversion rules...{C_RESET}\n")
    
    # Retrieve MAC address for Web UI credentials
    target_mac = ""
    try:
        child.sendline('cat /sys/class/net/br0/address')
        child.expect('# ')
        raw_mac = child.before.decode(errors='ignore').strip().split()[-1]
        if len(raw_mac) == 17 and ':' in raw_mac:
            target_mac = raw_mac.lower()
    except Exception:
        pass
    
    total = len(commands)
    for i, (desc, c) in enumerate(commands, 1):
        print(f" {C_BOLD}[{i}/{total}]{C_RESET} {desc}...")
        child.sendline(c)
        child.expect('# ')
        time.sleep(0.1)
        raw_output = child.before.decode(errors='ignore').strip().splitlines()
        # Extract verification outputs (e.g. PARAM=val or backup file listing)
        verified_lines = [
            line.strip() for line in raw_output 
            if ('=' in line and not line.strip().startswith(('flash ', 'port ', 'tr069 ', 'vlan ')))
            or ('hw.bin' in line and not line.strip().startswith('cp '))
        ]
        if verified_lines:
            details = ", ".join(verified_lines)
            print(f"      └─ {C_GREEN}Verified in Flash:{C_RESET} {details}")
        else:
            print(f"      └─ {C_GREEN}Done{C_RESET}")

    return target_mac

def wait_for_adapter_settled(parent_iface, had_default_route_before, max_wait=10):
    """Poll (don't just sleep-and-hope) for NetworkManager to finish reconnecting the
    adapter after we hand it back with managed=yes. Deliberately does NOT ping any
    external host (e.g. 1.1.1.1): the machine may have no WAN uplink at all when this
    script is run (isolated bench network, offline testing, etc.), and that is not
    something this script broke or is responsible for fixing. We only care whether the
    adapter's own state regressed relative to how it looked before we touched it.

    Returns True if either:
      - the adapter did NOT have a default route before we started (nothing to regress
        from, so there's nothing to verify), or
      - the adapter has a default route again now.
    """
    if not had_default_route_before:
        return True
    for _ in range(max_wait):
        res = subprocess.run(f"ip route show default dev {parent_iface}", shell=True, stdout=subprocess.PIPE, text=True)
        if res.stdout.strip():
            return True
        time.sleep(1)
    return False

def print_network_recovery_banner(parent_iface):
    print(f"\n{C_BOLD}{C_RED}" + "!" * 76)
    print("  WARNING: Your host's internet connection did not come back automatically")
    print("!" * 76 + f"{C_RESET}")
    print(f"{C_YELLOW}This adapter had a working default route before this script ran, but does not")
    print(f"have one now. Normally this script hands '{parent_iface}' back to NetworkManager")
    print(f"cleanly and this never happens — but if NetworkManager was unavailable, behaved")
    print(f"unexpectedly, or a connection profile is otherwise misconfigured, it can still")
    print(f"leave this adapter stuck on a broken connection profile — sometimes one named")
    print(f"exactly after the interface itself (e.g. '{parent_iface}') — with no working")
    print(f"DHCP/internet config. This has to be fixed by hand.{C_RESET}")
    print(f"\n{C_BOLD}Option A — GUI (Settings app):{C_RESET}")
    print(f"  1. Open {C_CYAN}Settings -> Network{C_RESET}")
    print(f"  2. Under 'Wired', you may see TWO profiles. If one is literally named")
    print(f"     '{parent_iface}', do NOT select it — click the OTHER profile's name")
    print(f"     (or its toggle) to connect that one instead.")
    print(f"  3. If only the '{parent_iface}' profile exists, click its gear icon and")
    print(f"     choose Remove/Forget, then unplug and replug the cable so")
    print(f"     NetworkManager auto-creates a fresh DHCP profile for it.")
    print(f"\n{C_BOLD}Option B — Command line:{C_RESET}")
    print(f"  {C_BOLD}nmcli con show{C_RESET}                              # list all profiles")
    print(f"  {C_BOLD}nmcli con up \"<your normal wired profile>\"{C_RESET}      # switch to the real one")
    print(f"  {C_BOLD}# --- or, if a bad profile literally named '{parent_iface}' exists ---{C_RESET}")
    print(f"  {C_BOLD}sudo nmcli con delete {parent_iface}{C_RESET}")
    print(f"  {C_BOLD}nmcli device connect {parent_iface}{C_RESET}")
    print(f"\n{C_YELLOW}This is a NetworkManager quirk on the HOST machine, not a problem with the{C_RESET}")
    print(f"{C_YELLOW}RMDU itself — the switch conversion above completed successfully regardless.{C_RESET}")
    print(f"{C_RED}{'!' * 76}{C_RESET}\n")

def print_banner():
    print(f"{C_BOLD}{C_BLUE}" + "=" * 76)
    print("      COMTREND / JIO RMDU (JRDU4311) ZERO-TOUCH SWITCH PROVISIONER")
    print("      Developers: junkmahn & Antigravity  |  Special Thanks: Pankaj2867")
    print("=" * 76 + f"{C_RESET}")
    print(f"{C_YELLOW}{C_BOLD}Important Port Wiring Rule for Fresh / Factory Units:{C_RESET}")
    print(f"  1. Connect your PC Ethernet cable specifically into {C_GREEN}{C_BOLD}PORT 1 (Yellow){C_RESET}.")
    print("     (Factory firmware restricts management VLAN 960 exclusively to Port 1).")
    print("  2. Ensure RMDU is powered on via PoE injector on ANY other Yellow port (Ports 2–7).")
    print(f"  3. {C_RED}{C_BOLD}NEVER connect your PC to Port 8 (Blue PoE Out)!{C_RESET}")
    print("-" * 76)

def main():
    if os.geteuid() != 0:
        print(f"{C_RED}[!] Error: Root privileges are required to configure VLAN network interfaces.{C_RESET}")
        print(f"    Please re-run with sudo: {C_BOLD}sudo {sys.argv[0]}{C_RESET}")
        sys.exit(1)
        
    print_banner()
    
    global SELECTED_PARENT_IFACE
    cli_arg = sys.argv[1] if len(sys.argv) > 1 else None
    parent_iface = select_interface_interactively(cli_arg)
    SELECTED_PARENT_IFACE = parent_iface
    
    print(f"\n{C_BOLD}[*] Selected adapter: {C_GREEN}{parent_iface}{C_RESET}")
    
    # Check link status
    carrier = read_sysfs(f"/sys/class/net/{parent_iface}/carrier") == "1"
    if not carrier:
        print(f"{C_YELLOW}[!] Warning: '{parent_iface}' reports NO physical link (cable unplugged or switch booting).{C_RESET}")
        input(f"    Plug the cable into any Yellow port and press {C_BOLD}[Enter]{C_RESET} to continue...")

    # Record whether this adapter had a working default route BEFORE we touch anything,
    # so that later we only warn the user about a REGRESSION we may have caused — not
    # about a lack of internet that was already the case before the script ran (e.g. on
    # an isolated bench machine with no WAN uplink at all).
    baseline_route_res = subprocess.run(f"ip route show default dev {parent_iface}", shell=True, stdout=subprocess.PIPE, text=True)
    had_default_route_before = bool(baseline_route_res.stdout.strip())

    # Surrender this device to our own control for the duration of the provisioning run.
    # NetworkManager will stop reacting to anything we do to it with raw `ip`/`vlan`
    # commands below, which is what prevents it from ever creating a bogus "assumed
    # connection" profile for this adapter in the first place.
    set_device_managed(parent_iface, False)

    # Step 1: Reachability check (VLAN vs Untagged)
    vlan_needed = False
    vlan_iface = None
    
    # Temporarily ensure parent interface has an IP in 192.168.33.0/24 to test untagged connectivity
    subprocess.run(f"ip addr add {HOST_VLAN_IP} dev {parent_iface} 2>/dev/null", shell=True)
    
    print(f"\n{C_CYAN}[*] Testing connection to RMDU ({RMDU_IP})...{C_RESET}")
    if check_ping(RMDU_IP, count=1, timeout=1):
        print(f"{C_GREEN}[+] RMDU reached directly via untagged Ethernet!{C_RESET}")
    else:
        # Untagged failed, remove temporary IP from parent interface
        subprocess.run(f"ip addr del {HOST_VLAN_IP} dev {parent_iface} 2>/dev/null", shell=True)
        print(f"[-] Untagged ping failed (expected on factory units).")
        vlan_needed = True
        vlan_iface = setup_vlan_interface(parent_iface)
        
        print(f"{C_CYAN}[*] Polling RMDU over VLAN {VLAN_ID} (waiting for switch boot)...{C_RESET}")
        online = False
        for attempt in range(1, 46):
            if check_ping(RMDU_IP, count=1, timeout=1):
                online = True
                print(f"\n{C_GREEN}[+] RMDU responded over VLAN {VLAN_ID} on attempt {attempt}!{C_RESET}")
                break
            sys.stdout.write(f"\r    Waiting for RMDU response... [{attempt}/45s]")
            sys.stdout.flush()
            time.sleep(1)
            
        if not online:
            print(f"\n{C_RED}[!] Failed to reach RMDU at {RMDU_IP} over VLAN {VLAN_ID}.{C_RESET}")
            print("    Troubleshooting steps:")
            print(f"    - {C_BOLD}{C_YELLOW}CRITICAL: Verify Ethernet cable is plugged into PORT 1 (Yellow).{C_RESET}")
            print("      In factory firmware, Ports 2–7 block VLAN 960 to isolate tenants!")
            print("    - Verify the RMDU PoE injector LED is lit on another port (e.g. Port 2).")
            print("    - Allow 45-60 seconds after power-on for Linux to finish booting.")
            teardown_vlan_interface(vlan_iface)
            sys.exit(1)

    # Step 2: Trigger SSH remote activation
    print(f"\n{C_BOLD}[Step 1/3] Activating Dropbear SSH over HTTP...{C_RESET}")
    trigger_http_endpoint("SSH_ENABLE")
    
    # Wait for TCP 22 to open
    ssh_ready = False
    for _ in range(10):
        if check_port_open(RMDU_IP, 22, timeout=1.0):
            ssh_ready = True
            break
        time.sleep(0.5)
        
    if not ssh_ready:
        print(f"{C_RED}[!] SSH port 22 did not open after HTTP trigger.{C_RESET}")
        if vlan_iface: teardown_vlan_interface(vlan_iface)
        sys.exit(1)
    print(f"{C_GREEN}[+] SSH service is UP and accepting connections!{C_RESET}")

    # Step 3: Run the full conversion pipeline
    print(f"\n{C_BOLD}[Step 2/3] Configuring Switch Hardware & Disabling Telemetry...{C_RESET}")
    conversion_pipeline = [
        ("Preserving Original Factory Configuration", "[ -f /hw_setting/hw.bin.orig ] || cp /hw_setting/hw.bin /hw_setting/hw.bin.orig; sync; ls -lh /hw_setting/hw.bin.orig"),
        ("Dismantling ASIC Port Isolation", "port -i 0; flash set PORT_ISOLATION_MASK_GROUP1 0; flash get PORT_ISOLATION_MASK_GROUP1"),
        ("Disabling Carrier VLANs in Flash MIB", "vlan -e 0; flash set VLAN_ENABLED 0; flash get VLAN_ENABLED"),
        ("Disabling TR-069 & CWMP Phone-Home", "tr069 set tr069_enable 0; flash set CWMP_ENABLED 0; flash set CWMP_INFORM_ENABLE 0; flash get CWMP_ENABLED; flash get CWMP_INFORM_ENABLE"),
        ("Disabling Jio MQTT Telemetry Daemon", "flash set MQTT_ENABLE 0; flash get MQTT_ENABLE"),
        ("Committing Permanent SSH/Telnet Network Access", "flash set SSH_ENABLE 1; flash set TELNET_ENABLE 1; flash get SSH_ENABLE; flash get TELNET_ENABLE"),
        ("Creating Persistent Flash Backup Copy", "cp /hw_setting/hw.bin /hw_setting/hw.bin.flat_switch_v2 2>/dev/null; sync; ls -lh /hw_setting/hw.bin.flat_switch_v2")
    ]
    
    target_mac = ""
    try:
        target_mac = execute_remote_ssh_pipeline(conversion_pipeline)
    except Exception as e:
        print(f"{C_RED}[!] Pipeline failed: {e}{C_RESET}")
        if vlan_iface: teardown_vlan_interface(vlan_iface)
        sys.exit(1)

    # Step 4: Issue Reboot and Clean Up
    print(f"\n{C_BOLD}[Step 3/3] Finalizing & Rebooting RMDU...{C_RESET}")
    try:
        child = pexpect.spawn(f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {RMDU_ROOT_USER}@{RMDU_IP}", timeout=5)
        child.expect('[Pp]assword:')
        child.sendline(RMDU_ROOT_PASS)
        child.expect('# ')
        child.sendline('reboot')
        child.expect(pexpect.EOF, timeout=3)
    except Exception:
        pass

    if vlan_iface:
        teardown_vlan_interface(vlan_iface)

    # Add secondary IP alias 192.168.33.2/24 to parent interface so host can ping
    # the flat switch untagged, WITHOUT replacing or disrupting any DHCP lease!
    subprocess.run(f"ip addr add {HOST_VLAN_IP} dev {parent_iface} 2>/dev/null", shell=True)

    print(f"{C_CYAN}[*] RMDU is rebooting into unmanaged Flat Gigabit Switch mode (~40–50 seconds)...{C_RESET}")
    
    # Wait for device to reappear untagged
    switch_online = False
    for i in range(1, 61):
        sys.stdout.write(f"\r    Waiting for switch to reboot... [{i}/60s]")
        sys.stdout.flush()
        time.sleep(1)
        if i >= 20:
            # Re-ensure 192.168.33.2 alias exists (in case NetworkManager DHCP reconfigured the interface)
            subprocess.run(f"ip addr add {HOST_VLAN_IP} dev {parent_iface} 2>/dev/null", shell=True)
            if check_ping(RMDU_IP, count=1, timeout=1):
                switch_online = True
                print(f"\n{C_GREEN}[+] Switch is back online and responding to management IP ({RMDU_IP})!{C_RESET}")
                break
                
            # Also check if switch bridged traffic to an upstream router on another port
            res_gw = subprocess.run(f"ip route show dev {parent_iface} default", shell=True, stdout=subprocess.PIPE, text=True)
            if "default via" in res_gw.stdout:
                gw_ip = res_gw.stdout.split()[2]
                if check_ping(gw_ip, count=1, timeout=1):
                    switch_online = True
                    print(f"\n{C_GREEN}[+] Switch is actively bridging! Acquired DHCP from router and reached gateway ({gw_ip})!{C_RESET}")
                    break

    if not switch_online:
        print(f"\n{C_YELLOW}[!] Notice: Switch reboot took longer than 60s to respond to ICMP.{C_RESET}")

    # Clean up temporary management IP from parent interface so main network/DHCP works seamlessly
    cleanup_host_network()

    print(f"{C_CYAN}[*] Waiting for NetworkManager to reconnect '{parent_iface}' normally...{C_RESET}")
    if wait_for_adapter_settled(parent_iface, had_default_route_before):
        if had_default_route_before:
            print(f"{C_GREEN}[+] Host adapter '{parent_iface}' confirmed back online with its normal connection.{C_RESET}")
        else:
            print(f"{C_GREEN}[+] Host adapter '{parent_iface}' handed back to NetworkManager.{C_RESET}")
    else:
        print_network_recovery_banner(parent_iface)

    mac_clean = target_mac.replace(':', '').lower() if target_mac else ""
    web_pass = f"JRDU4311{mac_clean}" if mac_clean else "JRDU4311<mac>"

    print(f"\n\n{C_BOLD}{C_GREEN}" + "=" * 76)
    print("                      CONVERSION SUCCESSFUL!")
    print("=" * 76 + f"{C_RESET}")
    print(f"{C_GREEN}Your Comtrend RMDU is now an unmanaged, full-duplex Gigabit switch!{C_RESET}")

    print(f"\n{C_BOLD}{C_CYAN}Management Access Credentials:{C_RESET}")
    print("-" * 76)
    print(f"  • {C_BOLD}Web Management UI:{C_RESET}")
    print(f"      URL:      {C_CYAN}http://192.168.33.1/{C_RESET}")
    print(f"      Username: {C_BOLD}admin{C_RESET}")
    print(f"      Password: {C_YELLOW}{C_BOLD}{web_pass}{C_RESET}   (Format: JRDU4311 + lowercase MAC)")
    print()
    print(f"  • {C_BOLD}SSH Console Shell (TCP 22):{C_RESET}")
    print(f"      Command:  {C_CYAN}ssh root@192.168.33.1{C_RESET}")
    print(f"      Username: {C_BOLD}root{C_RESET}")
    print(f"      Password: {C_YELLOW}{C_BOLD}YG*vd2!C2DL=LYXn3ni@{C_RESET}")
    print()
    print(f"  • {C_BOLD}Telnet Shell (TCP 23):{C_RESET}")
    print(f"      Command:  {C_CYAN}telnet 192.168.33.1{C_RESET}")
    print(f"      Username: {C_BOLD}root{C_RESET}")
    print(f"      Password: {C_YELLOW}{C_BOLD}YG*vd2!C2DL=LYXn3ni@{C_RESET}")

    print(f"\n{C_BOLD}{C_CYAN}How to Access the RMDU Web UI from Your Laptop:{C_RESET}")
    print("-" * 76)
    print("When connected to your home router, your laptop receives an IP like 192.168.1.x.")
    print("To reach http://192.168.33.1/ at the same time, add a TEMPORARY secondary IP")
    print(f"directly to the interface — this does NOT touch NetworkManager's saved")
    print(f"connection profile at all, so your normal DHCP/Internet connection is never")
    print(f"at risk, no matter what that profile is named:\n")
    print(f"  1. Run this command in your terminal (replace the interface name if needed):")
    print(f"     {C_BOLD}sudo ip addr add 192.168.33.2/24 dev {parent_iface}{C_RESET}\n")
    print(f"  2. Open your web browser and navigate to:")
    print(f"     {C_CYAN}{C_BOLD}http://192.168.33.1/{C_RESET}\n")
    print(f"  3. Log in with Username: {C_BOLD}admin{C_RESET} and Password: {C_BOLD}{web_pass}{C_RESET}\n")
    print(f"  4. When you're done, remove the temporary alias:")
    print(f"     {C_BOLD}sudo ip addr del 192.168.33.2/24 dev {parent_iface}{C_RESET}")
    print(f"\n{C_YELLOW}Do NOT run `nmcli con mod ... +ipv4.addresses` / `nmcli con up` against your{C_RESET}")
    print(f"{C_YELLOW}real wired connection profile to do this — that pattern is what caused this{C_RESET}")
    print(f"{C_YELLOW}tool's own internet-connectivity bug in earlier versions.{C_RESET}")

    print(f"\n{C_BOLD}{C_CYAN}Hardware Port Wiring:{C_RESET}")
    print("-" * 76)
    print("  • Ports 1–7 (Yellow): Connect home router, PCs, TVs, and APs (1 Gbps line rate).")
    print(f"  • Port 8 (Blue):      {C_RED}{C_BOLD}DO NOT connect normal client PCs{C_RESET} (provides 48V PoE Out).")
    print("=" * 76 + "\n")

if __name__ == "__main__":
    main()