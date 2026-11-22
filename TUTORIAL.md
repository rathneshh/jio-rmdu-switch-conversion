# Step-by-Step Conversion Tutorial: Comtrend / Jio RMDU (JRDU4311) to Gigabit Switch

> **Developers:** junkmahn & Antigravity  

This guide provides foolproof, step-by-step instructions to convert a factory-locked **Comtrend / Jio JRDU4311 RMDU** into an unmanaged, full-duplex 8-port Gigabit Ethernet switch directly over your network cable — **no hardware teardown, UART soldering, or case opening required**.

---

## Prerequisites

Before starting, ensure you have:
1. **The Comtrend / Jio RMDU (JRDU4311)** unit.
2. **PoE Injector** (The stock Jio POE adapter is rated **54V DC, 0.56A, 30.00W Max**; any standard 48V–54V 802.3at/af injector also works).
3. **Linux PC or Laptop** with an Ethernet port (or USB-to-Ethernet adapter).
4. **Two standard Ethernet patch cables** (Cat5e or Cat6).
5. **Python 3 & pexpect** installed on your Linux machine:
   ```bash
   sudo apt update && sudo apt install -y python3 python3-pexpect
   ```

---

## Physical Port Layout Overview

Looking at the RJ45 ports on the RMDU from left to right:

```
 [ Port 1 ] [ Port 2 ] [ Port 3 ] [ Port 4 ] [ Port 5 ] [ Port 6 ] [ Port 7 ]   [ Port 8 ]
 (YELLOW)   (YELLOW)   (YELLOW)   (YELLOW)   (YELLOW)   (YELLOW)   (YELLOW)     (BLUE)
    │                                                      │                    │
    └─ PC Connect (VLAN 960)                               └─ PoE Power IN      └─ Active PoE Out / Data
```

* **Ports 1–7 (Yellow):** 10/100/1000 Mbps Gigabit Data Ports with PoE-In capability.
* **Port 8 (Blue):** Gigabit Data Port with **Active 48V–54V PoE Output** (IEEE 802.3af/at PSE).  
  - **Non-PoE Devices (PCs, Laptops, Consoles):** Completely safe to connect! The internal TI eFuse hardware controller automatically detects non-PoE devices and leaves power OFF (0V), acting as a normal 1 Gbps Gigabit data port.
  - **PoE Devices (APs, IP Cameras, VoIP Phones):** Supplies active 48V–54V PoE power (up to ~15W–18W with the stock 30W adapter).
  - **Caution:** Do NOT connect 24V passive PoE gear (e.g. older airMAX antennas) as they do not support 48V–54V active voltage.

---

## Step 1: Physical Hardware Cabling

Follow these exact wiring steps:

1. **Power the RMDU via PoE:**
   * Plug your PoE power injector into mains electricity.
   * Take an Ethernet cable from the injector's **POE / POWER OUT** port.
   * Plug it into the **rightmost Yellow port (Port 7)**.  
     *(You can use any Yellow port between 2 and 7, but Port 7 is recommended to keep cables neatly separated).*
   * **Do NOT plug the PoE injector into Port 1 yet.**

2. **Connect your Linux PC to Port 1:**
   * Take your second Ethernet cable.
   * Plug one end into your **Linux PC / laptop Ethernet port**.
   * Plug the other end specifically into the **leftmost Yellow port (Port 1)**.
   * *Why Port 1?* Factory firmware isolates the management network (VLAN 960) exclusively to Port 1 in hardware. Ports 2–7 strictly block management traffic on factory units.

3. **Wait 45–60 Seconds for Boot:**
   * Allow the RMDU to finish its internal Linux boot sequence. The green Ethernet LEDs on Port 1 and Port 7 will illuminate.

---

## Step 2: Prepare the Provisioning Script

1. Open a terminal on your Linux computer and navigate to the directory containing the conversion scripts:
   ```bash
   cd ~/jio-rmdu-switch-conversion
   ```

2. Make the scripts executable:
   ```bash
   chmod +x provision_new_rmdu.py factory_reset_rmdu.py
   ```

---

## Step 3: Run the Zero-Touch Conversion Script

1. Launch the provisioning utility with root privileges:
   ```bash
   sudo ./provision_new_rmdu.py
   ```

2. **Select your Ethernet adapter:**
   The script scans your network cards and automatically detects which adapter has an active link connected to the RMDU:
   ```text
   Available Network Adapters:
   ============================================================================
    #   Interface         Type       Link Status              Speed     
   ----------------------------------------------------------------------------
    [1] eth0              Ethernet   LINK UP (Connected)      1000 Mbps  [RECOMMENDED]
        └─ MAC: 00:11:22:33:44:55
    [2] wlan0             Wi-Fi      LINK UP (Connected)                
        └─ MAC: aa:bb:cc:dd:ee:ff
   ============================================================================
   Select adapter [1-2] (default: 1): 
   ```
   Simply press **`Enter`** to accept the recommended adapter.

3. **Sit back and watch the automation:**
   The script performs the complete conversion automatically:
   * **VLAN 960 Setup:** Creates an 802.1Q subinterface (`vlan960`) with IP `192.168.33.2/24`.
   * **SSH Activation:** Sends an HTTP trigger to `/SSH_ENABLE` to start Dropbear SSH.
   * **Hardware Reconfiguration & Read-After-Write Verification:**
     * Removes ASIC port isolation (`PORT_ISOLATION_MASK_GROUP1=0`).
     * Disables carrier VLAN tags (`VLAN_ENABLED=0`).
     * Kills TR-069 & CWMP phone-home (`CWMP_ENABLED=0`).
     * Disables Jio MQTT telemetry daemons (`MQTT_ENABLE=0`).
     * Permanently enables Dropbear SSH and Telnet (`SSH_ENABLE=1`, `TELNET_ENABLE=1`).
     * Creates a persistent backup configuration in `/hw_setting/hw.bin.flat_switch_v2`.
   * **Reboot:** Sends the `reboot` command, tears down the temporary VLAN interface, and waits ~35 seconds for the switch to reboot into flat mode.
   * **Host Network Restoration:** Automatically restores your PC adapter to normal DHCP mode.

When finished, you will see:
```text
============================================================================
                      CONVERSION SUCCESSFUL!
============================================================================
Your Comtrend RMDU is now an unmanaged, full-duplex Gigabit switch!
```

---

## Step 4: Connect to Your Home Network & Verify

Your RMDU is now a standard, unmanaged 8-port Gigabit switch. To integrate it into your home setup:

1. **Connect your Home Router to the Switch:**
   * Take an Ethernet cable from a LAN port on your main home router.
   * Plug it into the **LAN / DATA IN** port on the PoE injector.
   * *(Because the PoE injector's POWER OUT port is plugged into Port 7, your home router's LAN is now bridged into Port 7 of the switch!)*

2. **Connect Your Client Devices:**
   * Connect PCs, laptops, gaming consoles, smart TVs, or Wi-Fi Access Points to **any Yellow port (Ports 1 through 7)**.
   * All ports will forward data between each other and your router at full 1 Gbps wirespeed.

3. **Verify Connectivity on Your PC:**
   Verify that your PC received an IP address from your home router and has full Internet access:
   ```bash
   # Check acquired DHCP IP
   ip addr show

   # Test Internet connectivity
   ping -c 3 1.1.1.1
   ```
   You should see 0% packet loss and low latency.

---

## Step 5: (Optional) Accessing the RMDU Web Management UI

The RMDU retains a built-in web management interface at `http://192.168.33.1/` where you can view port link speeds, traffic counters, MAC address tables, and port mirroring.

Because your home router uses a different subnet (e.g., `192.168.1.x`), you can add a temporary secondary IP alias to access the RMDU without interrupting your internet:

1. **Add a temporary IP alias (replace `eth0` with your Ethernet interface name):**
   ```bash
   sudo ip addr add 192.168.33.2/24 dev eth0
   ```

2. **Open your web browser:**
   Navigate to:
   ```
   http://192.168.33.1/
   ```

3. **Log in:**
   * **Username:** `admin`
   * **Password:** `JRDU4311<mac>` *(where `<mac>` is your RMDU's MAC address in lowercase without colons, e.g. `JRDU4311a01842xxxxxx`, printed on the completion screen)*.

4. **SSH Root Shell Access:**
   You can also SSH into the switch at any time:
   ```bash
   ssh root@192.168.33.1
   # Password: YG*vd2!C2DL=LYXn3ni@
   ```

5. **Remove the temporary alias when finished:**
   ```bash
   sudo ip addr del 192.168.33.2/24 dev eth0
   ```

---

## Step 6: (Optional) Backups & Factory Reset

### Creating a Configuration Backup
To back up the active flash image and human-readable MIB parameters at any time:
```bash
./factory_reset_rmdu.py --backup
```
> [!NOTE]
> The tool safely backs up the filesystem configuration (`/hw_setting/hw.bin` and `flash all`). **Do NOT attempt to read or `dd` raw `/dev/mtd*` block devices (especially `/dev/mtd0`)** — doing so trips unmapped ECC blocks in the SPI NAND controller and will cause a kernel panic.

### Reverting to Factory Stock
If you ever wish to re-lock the unit back to its original Jio factory state (e.g., for testing or return):

```bash
./factory_reset_rmdu.py --factory
```

This restores stock carrier VLANs, port isolation, and telemetry, returning the unit to its pristine out-of-the-box state.
