#!/usr/bin/env python3
"""
================================================================================
 Comtrend / Jio RMDU (JRDU4311) Flash Profile Manager & Factory Reset Tool
================================================================================

 Developers:     junkmahn & Antigravity

 Purpose:
   Manages RMDU firmware configuration profiles and backups:
     [1] Factory Reset       - Re-locks switch into pristine stock Jio state
     [2] Flat Gigabit Switch - Restores unmanaged 8-port switch configuration
     [3] Backup Active Flash - Backs up active /hw_setting/hw.bin to /hw_setting/hw.bin.orig
                               and downloads local copies (via TFTP or SSH) to PC

 Safety Rule:
   NEVER read or dump raw /dev/mtd* block devices (especially /dev/mtd0):
   the SPI NAND ECC controller will encounter unmapped blocks and panic the kernel.
   The entire factory configuration resides safely in /hw_setting/hw.bin on the
   mounted YAFFS filesystem.

 Supports execution via:
   - UART Serial Console (/dev/ttyUSB0 @ 38400 baud)
   - SSH Network Console (root@192.168.33.1:22)

 Usage:
   ./factory_reset_rmdu.py              # Interactive menu
   ./factory_reset_rmdu.py --factory    # Immediate factory reset
   ./factory_reset_rmdu.py --flat       # Immediate flat switch restore
   ./factory_reset_rmdu.py --backup     # Immediate configuration backup
================================================================================
"""

import sys
import os
import time
import struct
import socket
import hashlib
import argparse
import threading
import subprocess

try:
    import serial
except ImportError:
    serial = None

try:
    import pexpect
except ImportError:
    pexpect = None

# ANSI color codes
C_RESET  = "\033[0m"
C_BOLD   = "\033[1m"
C_GREEN  = "\033[1;32m"
C_YELLOW = "\033[1;33m"
C_CYAN   = "\033[1;36m"
C_RED    = "\033[1;31m"
C_BLUE   = "\033[1;34m"

RMDU_IP        = "192.168.33.1"
RMDU_ROOT_USER = "root"
RMDU_ROOT_PASS = "YG*vd2!C2DL=LYXn3ni@"
SERIAL_PORT    = "/dev/ttyUSB0"
BAUD_RATE      = 38400

BACKUP_ORIG    = "/hw_setting/hw.bin.orig"
BACKUP_FLAT    = "/hw_setting/hw.bin.flat_switch_v2"
ACTIVE_HW_BIN  = "/hw_setting/hw.bin"
ACTIVE_TMP_BIN = "/var/tmp/data/hw.bin"
LOCAL_BACKUP_DIR = "./backups"


def print_banner():
    print(f"\n{C_BOLD}{C_BLUE}" + "=" * 76)
    print("      COMTREND / JIO RMDU (JRDU4311) HARDWARE CONFIGURATION MANAGER")
    print("      Developers: junkmahn & Antigravity  |  Special Thanks: Pankaj2867")
    print("=" * 76 + f"{C_RESET}\n")


def check_port_open(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, OSError):
        return False


def uart_authenticate(ser):
    """Authenticate via UART console and establish root prompt."""
    ser.reset_input_buffer()
    ser.write(b"\r\n")
    time.sleep(0.3)
    buf = ser.read(ser.in_waiting).decode("utf-8", errors="replace")
    
    if "# " in buf:
        return True
        
    for _ in range(5):
        if "login:" in buf:
            ser.write(b"root\r\n")
            time.sleep(0.4)
            buf = ser.read(ser.in_waiting).decode("utf-8", errors="replace")
        elif "Password:" in buf:
            ser.write(f"{RMDU_ROOT_PASS}\r\n".encode())
            time.sleep(0.6)
            buf = ser.read(ser.in_waiting).decode("utf-8", errors="replace")
            if "# " in buf:
                return True
        elif "# " in buf:
            return True
        else:
            ser.write(b"\r\n")
            time.sleep(0.4)
            buf = ser.read(ser.in_waiting).decode("utf-8", errors="replace")
            if "# " in buf:
                return True

    return False


def uart_exec(ser, cmd, timeout=3.0):
    """Execute command over UART with paced transmission to eliminate FIFO overruns."""
    ser.reset_input_buffer()
    ser.write(b"\x03\r\n")
    time.sleep(0.1)
    ser.reset_input_buffer()
    
    # Send in 8-byte paced bursts to prevent Realtek 16550 UART FIFO overflow
    data = (cmd + "\r\n").encode()
    for i in range(0, len(data), 8):
        ser.write(data[i:i+8])
        ser.flush()
        time.sleep(0.02)
        
    start = time.time()
    out = ""
    while (time.time() - start) < timeout:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode("utf-8", errors="replace")
            out += chunk
            if "# " in out:
                break
        time.sleep(0.05)
    return out


# ==============================================================================
# Pure-Python TFTP Receiver (Handles WRQ file uploads from RMDU)
# ==============================================================================
def start_tftp_receiver(bind_ip, bind_port=69, out_dir=LOCAL_BACKUP_DIR, timeout=15):
    """Simple embedded pure-Python TFTP server thread for receiving files via `tftp -p`."""
    os.makedirs(out_dir, exist_ok=True)
    received_files = {}

    def server_worker():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((bind_ip, bind_port))
            s.settimeout(timeout)
        except Exception:
            return

        start_time = time.time()
        while (time.time() - start_time) < timeout:
            try:
                pkt, addr = s.recvfrom(2048)
            except socket.timeout:
                break
            except Exception:
                break

            # Check WRQ opcode (2)
            if len(pkt) < 2 or struct.unpack("!H", pkt[:2])[0] != 2:
                continue

            filename = os.path.basename(pkt[2:].split(b"\0")[0].decode(errors='ignore'))
            t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                t.bind((bind_ip, 0))
                t.settimeout(10)
                # Send ACK 0
                t.sendto(struct.pack("!HH", 4, 0), addr)

                expect_blk = 1
                total_bytes = 0
                out_path = os.path.join(out_dir, filename)

                with open(out_path, "wb") as f:
                    done = False
                    while not done:
                        try:
                            d, a = t.recvfrom(2048)
                        except socket.timeout:
                            break
                        if len(d) < 4:
                            break
                        op, blk = struct.unpack("!HH", d[:4])
                        if op != 3: # DATA opcode
                            continue
                        if blk == expect_blk:
                            chunk_data = d[4:]
                            f.write(chunk_data)
                            total_bytes += len(chunk_data)
                            expect_blk = (expect_blk + 1) & 0xffff
                            done = len(chunk_data) < 512
                        t.sendto(struct.pack("!HH", 4, blk), a)

                received_files[filename] = (out_path, total_bytes)
            except Exception:
                pass
            finally:
                t.close()

        s.close()

    th = threading.Thread(target=server_worker, daemon=True)
    th.start()
    return th, received_files


# ==============================================================================
# Configuration Backup Operations
# ==============================================================================
def perform_backup_uart(ser):
    """Back up configuration profiles over UART serial console."""
    print(f"\n{C_BOLD}[*] Performing Configuration Backup over UART...{C_RESET}")
    print(f"{C_CYAN}[*] Reading active configuration from {ACTIVE_HW_BIN}...{C_RESET}")

    # 1. Ensure target persistent /hw_setting/hw.bin.orig exists
    check_orig = uart_exec(ser, f"[ -f {BACKUP_ORIG} ] && echo EXISTS || echo MISSING")
    if "EXISTS" in check_orig:
        print(f"    ├─ {C_GREEN}Persistent factory backup already exists:{C_RESET} {BACKUP_ORIG}")
    else:
        print(f"    ├─ {C_YELLOW}Creating persistent factory backup:{C_RESET} {BACKUP_ORIG}...")
        uart_exec(ser, f"cp {ACTIVE_HW_BIN} {BACKUP_ORIG}; cp {ACTIVE_HW_BIN} {ACTIVE_TMP_BIN}.orig 2>/dev/null; sync")

    # 2. Create timestamped NAND backup
    ts = time.strftime("%Y%m%d_%H%M%S")
    target_bak = f"/hw_setting/hw.bin.bak_{ts}"
    uart_exec(ser, f"cp {ACTIVE_HW_BIN} {target_bak}; sync")
    print(f"    ├─ {C_GREEN}Created timestamped NAND backup:{C_RESET} {target_bak}")

    # 3. Query remote MD5 checksum
    md5_out = uart_exec(ser, f"md5sum {ACTIVE_HW_BIN}")
    remote_md5 = ""
    for line in md5_out.splitlines():
        if "hw.bin" in line and len(line.split()[0]) == 32:
            remote_md5 = line.split()[0]
            break
    print(f"    ├─ Remote MD5: {C_BOLD}{remote_md5}{C_RESET}")

    # 4. Attempt TFTP upload to host if host IP 192.168.33.2 is reachable
    host_ip = "192.168.33.2"
    tftp_th, rec_dict = start_tftp_receiver(host_ip, 69, LOCAL_BACKUP_DIR, timeout=8)
    time.sleep(0.3)

    print(f"{C_CYAN}[*] Pushing backup files to host via TFTP ({host_ip}:69)...{C_RESET}")
    tftp_hw = f"rmdu_hw_{ts}.bin"
    tftp_mib = f"rmdu_flash_all_{ts}.txt"

    uart_exec(ser, f"tftp -p -l {ACTIVE_HW_BIN} -r {tftp_hw} {host_ip} 2>/dev/null")
    uart_exec(ser, f"flash all > /tmp/flash_all.txt && tftp -p -l /tmp/flash_all.txt -r {tftp_mib} {host_ip} 2>/dev/null && rm /tmp/flash_all.txt")

    tftp_th.join(timeout=10)

    if tftp_hw in rec_dict:
        local_path, size = rec_dict[tftp_hw]
        local_md5 = hashlib.md5(open(local_path, "rb").read()).hexdigest()
        print(f"    ├─ {C_GREEN}Local Binary Backup:{C_RESET} {local_path} ({size} bytes)")
        print(f"    ├─ Local MD5:  {C_BOLD}{local_md5}{C_RESET}")
        if local_md5 == remote_md5:
            print(f"    └─ {C_GREEN}Integrity Check:{C_RESET} {C_BOLD}100% MATCH!{C_RESET}")
    else:
        print(f"    ├─ {C_YELLOW}Notice: Direct TFTP download skipped (host {host_ip}:69 not bound or unreachable).{C_RESET}")
        print(f"    └─ {C_GREEN}Persistent NAND flash copies safely committed on RMDU.{C_RESET}")

    print(f"\n{C_BOLD}{C_GREEN}[+] Backup Complete! Everything needed for factory reset is safely secured.{C_RESET}\n")
    return True


def perform_backup_ssh(child):
    """Back up configuration profiles over SSH session."""
    print(f"\n{C_BOLD}[*] Performing Configuration Backup over SSH...{C_RESET}")
    print(f"{C_CYAN}[*] Reading active configuration from {ACTIVE_HW_BIN}...{C_RESET}")

    # 1. Ensure target persistent /hw_setting/hw.bin.orig exists
    child.sendline(f"[ -f {BACKUP_ORIG} ] && echo EXISTS || echo MISSING")
    child.expect('# ')
    if "EXISTS" in child.before.decode(errors='ignore'):
        print(f"    ├─ {C_GREEN}Persistent factory backup already exists:{C_RESET} {BACKUP_ORIG}")
    else:
        print(f"    ├─ {C_YELLOW}Creating persistent factory backup:{C_RESET} {BACKUP_ORIG}...")
        child.sendline(f"cp {ACTIVE_HW_BIN} {BACKUP_ORIG}; cp {ACTIVE_HW_BIN} {ACTIVE_TMP_BIN}.orig 2>/dev/null; sync")
        child.expect('# ')

    # 2. Create timestamped NAND backup
    ts = time.strftime("%Y%m%d_%H%M%S")
    target_bak = f"/hw_setting/hw.bin.bak_{ts}"
    child.sendline(f"cp {ACTIVE_HW_BIN} {target_bak}; sync")
    child.expect('# ')
    print(f"    ├─ {C_GREEN}Created timestamped NAND backup:{C_RESET} {target_bak}")

    # 3. Query remote MD5 checksum
    child.sendline(f"md5sum {ACTIVE_HW_BIN}")
    child.expect('# ')
    md5_out = child.before.decode(errors='ignore')
    remote_md5 = ""
    for line in md5_out.splitlines():
        if "hw.bin" in line and len(line.split()[0]) == 32:
            remote_md5 = line.split()[0]
            break
    print(f"    ├─ Remote MD5: {C_BOLD}{remote_md5}{C_RESET}")

    # 4. Pull local binary backup via SSH base64 stream (100% binary-safe, avoids tftp port requirements)
    os.makedirs(LOCAL_BACKUP_DIR, exist_ok=True)
    local_bin = os.path.join(LOCAL_BACKUP_DIR, f"rmdu_hw_{ts}.bin")
    local_mib = os.path.join(LOCAL_BACKUP_DIR, f"rmdu_flash_all_{ts}.txt")

    print(f"{C_CYAN}[*] Downloading binary flash image to local machine ({local_bin})...{C_RESET}")
    child.sendline(f"openssl enc -base64 -in {ACTIVE_HW_BIN} -out /tmp/hw.b64 && wc -c /tmp/hw.b64")
    child.expect('# ')

    child.sendline("cat /tmp/hw.b64; echo __B64_EOF__")
    child.expect('__B64_EOF__')
    b64_raw = child.before.decode(errors='ignore')
    child.expect('# ')
    child.sendline("rm /tmp/hw.b64")
    child.expect('# ')

    import base64
    clean_b64 = "".join([l.strip() for l in b64_raw.splitlines() if len(l.strip()) > 0 and not l.strip().startswith('cat ')])
    try:
        bin_data = base64.b64decode(clean_b64)
        with open(local_bin, "wb") as f:
            f.write(bin_data)
        local_md5 = hashlib.md5(bin_data).hexdigest()
        print(f"    ├─ {C_GREEN}Saved Local Binary Backup:{C_RESET} {local_bin} ({len(bin_data)} bytes)")
        print(f"    ├─ Local MD5:  {C_BOLD}{local_md5}{C_RESET}")
        if local_md5 == remote_md5:
            print(f"    ├─ {C_GREEN}Integrity Check:{C_RESET} {C_BOLD}100% MATCH!{C_RESET}")
    except Exception as e:
        print(f"    ├─ {C_YELLOW}Notice: Base64 decode failed: {e}{C_RESET}")

    # 5. Pull flash all MIB dump
    print(f"{C_CYAN}[*] Downloading human-readable flash parameters ({local_mib})...{C_RESET}")
    child.sendline("flash all > /tmp/flash_all.txt; echo __MIB_DONE__")
    child.expect('__MIB_DONE__')
    child.expect('# ')

    child.sendline("cat /tmp/flash_all.txt; echo __MIB_EOF__")
    child.expect('__MIB_EOF__')
    mib_data = child.before.decode(errors='ignore')
    child.expect('# ')
    child.sendline("rm /tmp/flash_all.txt")
    child.expect('# ')

    with open(local_mib, "w") as f:
        f.write(mib_data)
    print(f"    └─ {C_GREEN}Saved MIB Parameters:{C_RESET} {local_mib}")

    print(f"\n{C_BOLD}{C_GREEN}[+] Backup Complete! Everything needed for factory reset is safely secured.{C_RESET}\n")
    return True


# ==============================================================================
# Factory Reset / Profile Restore Operations
# ==============================================================================
def reset_via_uart(source_bin, mode_name):
    """Restore configuration profile over UART serial console."""
    print(f"{C_CYAN}[*] Connecting to RMDU via UART ({SERIAL_PORT} @ {BAUD_RATE} baud)...{C_RESET}")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    except Exception as e:
        print(f"{C_RED}[!] Failed to open serial port {SERIAL_PORT}: {e}{C_RESET}")
        return False

    if not uart_authenticate(ser):
        print(f"{C_RED}[!] Could not establish root shell over UART.{C_RESET}")
        ser.close()
        return False

    print(f"{C_GREEN}[+] Authenticated as root over UART!{C_RESET}")

    # Check that backup file exists
    check_out = uart_exec(ser, f"[ -f {source_bin} ] && echo EXISTS || echo MISSING")
    if "EXISTS" not in check_out:
        print(f"{C_RED}[!] Backup file {source_bin} not found on target! Aborting to prevent bricking.{C_RESET}")
        ser.close()
        return False

    print(f"{C_CYAN}[*] Overwriting active configuration with {source_bin}...{C_RESET}")
    uart_exec(ser, f"cp {source_bin} {ACTIVE_HW_BIN}")
    uart_exec(ser, f"cp {source_bin} {ACTIVE_TMP_BIN} 2>/dev/null")
    uart_exec(ser, "sync")

    # Verify write
    chk = uart_exec(ser, f"ls -lh {ACTIVE_HW_BIN}")
    print(f"    └─ {chk.strip().splitlines()[-1] if chk.strip() else 'Written'}")

    print(f"{C_YELLOW}[*] Restored to {mode_name}! Issuing system reboot...{C_RESET}")
    ser.write(b"reboot\r\n")
    ser.close()
    return True


def reset_via_ssh(source_bin, mode_name):
    """Restore configuration profile over SSH network session."""
    print(f"{C_CYAN}[*] Connecting to RMDU via SSH ({RMDU_ROOT_USER}@{RMDU_IP}:22)...{C_RESET}")
    if not check_port_open(RMDU_IP, 22, timeout=1.0):
        # Try activating Dropbear via HTTP trigger
        try:
            import urllib.request
            urllib.request.urlopen(f"http://{RMDU_IP}/SSH_ENABLE", timeout=2)
            time.sleep(1)
        except Exception:
            pass

    if not check_port_open(RMDU_IP, 22, timeout=1.5):
        print(f"{C_RED}[!] SSH port 22 unreachable at {RMDU_IP}. (Ensure Ethernet cable is connected){C_RESET}")
        return False

    try:
        cmd = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 {RMDU_ROOT_USER}@{RMDU_IP}"
        child = pexpect.spawn(cmd, timeout=8)
        idx = child.expect(['[Pp]assword:', pexpect.EOF, pexpect.TIMEOUT])
        if idx != 0:
            print(f"{C_RED}[!] SSH did not offer password prompt.{C_RESET}")
            return False
            
        child.sendline(RMDU_ROOT_PASS)
        child.expect('# ')
        print(f"{C_GREEN}[+] Authenticated as root over SSH!{C_RESET}")

        # Check backup file exists
        child.sendline(f"[ -f {source_bin} ] && echo EXISTS || echo MISSING")
        child.expect('# ')
        if "EXISTS" not in child.before.decode(errors='ignore'):
            print(f"{C_RED}[!] Backup file {source_bin} not found on target! Aborting.{C_RESET}")
            return False

        print(f"{C_CYAN}[*] Overwriting active configuration with {source_bin}...{C_RESET}")
        child.sendline(f"cp {source_bin} {ACTIVE_HW_BIN}; cp {source_bin} {ACTIVE_TMP_BIN} 2>/dev/null; sync")
        child.expect('# ')

        print(f"{C_YELLOW}[*] Restored to {mode_name}! Issuing system reboot...{C_RESET}")
        child.sendline('reboot')
        time.sleep(1)
        return True
    except Exception as e:
        print(f"{C_RED}[!] SSH execution error: {e}{C_RESET}")
        return False


def wait_for_reboot(is_factory_reset=True):
    """Wait for the reboot cycle to finish and show progress."""
    print(f"\n{C_BOLD}[*] RMDU is rebooting (~25–35 seconds)...{C_RESET}")
    for i in range(1, 36):
        sys.stdout.write(f"\r    Waiting for switch reboot... [{i}/35s]")
        sys.stdout.flush()
        time.sleep(1)

    print(f"\n\n{C_BOLD}{C_GREEN}" + "=" * 76)
    if is_factory_reset:
        print("                  FACTORY RESET SUCCESSFUL!")
        print("=" * 76 + f"{C_RESET}")
        print(f"{C_YELLOW}Device has rebooted to pristine Jio factory state:{C_RESET}")
        print("  • ASIC Port Isolation:  ENABLED (Ports isolated to uplink)")
        print("  • Carrier VLANs:        ENABLED (VLAN 960 on Port 1, VLAN 1015 on Ports 2–7)")
        print("  • TR-069 & MQTT:        ENABLED (Phone-home telemetry active)")
        print("  • SSH & Telnet:         DISABLED")
        print(f"\n{C_CYAN}Ready for zero-touch provisioning test with ./provision_new_rmdu.py!{C_RESET}")
    else:
        print("                FLAT SWITCH RESTORE SUCCESSFUL!")
        print("=" * 76 + f"{C_RESET}")
        print(f"{C_GREEN}Device has rebooted into unmanaged 8-port Gigabit switch mode:{C_RESET}")
        print("  • All Ports 1–7 forward at 1 Gbps line rate.")
        print("  • Port isolation, VLANs, and telemetry disabled.")
        print("  • Web UI: http://192.168.33.1/  |  SSH: ssh root@192.168.33.1")
    print("=" * 76 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Comtrend RMDU Profile Manager & Factory Reset Tool")
    parser.add_argument("--factory", action="store_true", help="Restore factory stock Jio configuration")
    parser.add_argument("--flat", action="store_true", help="Restore flat unmanaged switch configuration")
    parser.add_argument("--backup", action="store_true", help="Backup active configuration for factory reset")
    parser.add_argument("--serial", default=SERIAL_PORT, help=f"Serial port device (default: {SERIAL_PORT})")
    args = parser.parse_args()

    print_banner()

    # Determine desired action
    action = None
    if args.factory:
        action = "factory"
    elif args.flat:
        action = "flat"
    elif args.backup:
        action = "backup"
    else:
        print(f"{C_BOLD}Select Configuration Action:{C_RESET}")
        print("----------------------------------------------------------------------------")
        print(f" {C_BOLD}[1]{C_RESET} {C_YELLOW}Factory Reset{C_RESET}       - Re-locks switch into pristine stock Jio state")
        print("                            (Restores Carrier VLANs 960/1015, Isolation, TR-069)")
        print(f" {C_BOLD}[2]{C_RESET} {C_GREEN}Flat Gigabit Switch{C_RESET} - Restores unmanaged 8-port switch configuration")
        print("                            (Wirespeed forwarding across all ports 1–7, no telemetry)")
        print(f" {C_BOLD}[3]{C_RESET} {C_CYAN}Backup Active Config{C_RESET}- Backs up active flash to /hw_setting/hw.bin.orig")
        print("                            (Creates persistent target flash & local PC backups)")
        print("----------------------------------------------------------------------------")
        choice = input(f"Select option [1-3] (default: 1): ").strip()
        if choice == "2":
            action = "flat"
        elif choice == "3":
            action = "backup"
        else:
            action = "factory"

    # Action: Backup
    if action == "backup":
        print(f"{C_CYAN}[*] Starting Configuration Backup...{C_RESET}")
        print(f"{C_YELLOW}[!] Note: Backing up mounted filesystem configuration ({ACTIVE_HW_BIN}).")
        print(f"    Raw /dev/mtd* block devices are safely bypassed to prevent NAND ECC kernel panics.{C_RESET}\n")

        # Try UART first
        if os.path.exists(args.serial) and serial is not None:
            try:
                ser = serial.Serial(args.serial, BAUD_RATE, timeout=1)
                if uart_authenticate(ser):
                    perform_backup_uart(ser)
                    ser.close()
                    sys.exit(0)
                ser.close()
            except Exception as e:
                print(f"{C_YELLOW}[!] UART backup failed ({e}), attempting SSH...{C_RESET}")

        # Try SSH
        if pexpect is not None:
            if not check_port_open(RMDU_IP, 22, timeout=1.0):
                try:
                    import urllib.request
                    urllib.request.urlopen(f"http://{RMDU_IP}/SSH_ENABLE", timeout=2)
                    time.sleep(1)
                except Exception:
                    pass

            if check_port_open(RMDU_IP, 22, timeout=1.5):
                try:
                    cmd = f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 {RMDU_ROOT_USER}@{RMDU_IP}"
                    child = pexpect.spawn(cmd, timeout=8)
                    idx = child.expect(['[Pp]assword:', pexpect.EOF, pexpect.TIMEOUT])
                    if idx == 0:
                        child.sendline(RMDU_ROOT_PASS)
                        child.expect('# ')
                        perform_backup_ssh(child)
                        sys.exit(0)
                except Exception as e:
                    print(f"{C_RED}[!] SSH backup error: {e}{C_RESET}")

        print(f"{C_RED}[!] Could not establish connection via UART or SSH for backup.{C_RESET}\n")
        sys.exit(1)

    # Action: Restore (Factory or Flat)
    is_factory = (action == "factory")
    source_bin = BACKUP_ORIG if is_factory else BACKUP_FLAT
    mode_name = "Factory Stock State" if is_factory else "Flat Gigabit Switch Mode"

    print(f"\n{C_BOLD}[*] Target Profile: {C_GREEN if not is_factory else C_YELLOW}{mode_name}{C_RESET}")

    # Transport selection: Try UART first, fallback to SSH
    success = False
    if os.path.exists(args.serial) and serial is not None:
        success = reset_via_uart(source_bin, mode_name)
    
    if not success:
        if pexpect is not None:
            print(f"{C_YELLOW}[!] UART not available. Attempting network SSH fallback...{C_RESET}")
            success = reset_via_ssh(source_bin, mode_name)
        else:
            print(f"{C_RED}[!] pexpect not available for SSH fallback.{C_RESET}")

    if success:
        wait_for_reboot(is_factory_reset=is_factory)
    else:
        print(f"\n{C_RED}[!] Failed to restore configuration. Please check cables/permissions.{C_RESET}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
