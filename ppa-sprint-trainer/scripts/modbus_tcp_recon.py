"""Network recon: is the SV-X3E drive reachable directly via Modbus TCP on
the workshop LAN?

Hypothesis: HMI and drive share an aftermarket gigabit switch. If the drive
has a Modbus TCP server on its network port, we can talk to it without CN3
USB at all — eliminating the CN3↔HMI mutual exclusion.

Strictly READ-ONLY. No writes anywhere.

Steps:
1. Identify our own IP on the 192.168.88.x subnet.
2. TCP-scan 192.168.88.1..254 on port 502 (Modbus TCP) with a short timeout.
3. For every host with port 502 open, try Modbus TCP read of P21.00 (status)
   across device IDs 1..5. Log whatever responds.
4. Print a summary.

Safety:
- Doesn't touch any physical interface.
- Doesn't write any registers.
- Doesn't depend on CN3 — works over LAN only.
"""
import concurrent.futures
import socket
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    from pymodbus.client import ModbusTcpClient
    HAVE_MODBUS = True
except ImportError:
    HAVE_MODBUS = False

SUBNET_PREFIX = "192.168.88."
PORT = 502
TCP_TIMEOUT = 0.6
HMI_IP = "192.168.88.222"
P21_00 = 0x1500


def my_lan_ip(target_ip: str = "192.168.88.222") -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 80))
        return s.getsockname()[0]
    finally:
        s.close()


def tcp_open(ip: str, port: int, timeout: float = TCP_TIMEOUT) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def scan_subnet():
    targets = [f"{SUBNET_PREFIX}{i}" for i in range(1, 255)]
    open_hosts = []
    print(f"Scanning {SUBNET_PREFIX}1..254 on port {PORT} (timeout {TCP_TIMEOUT}s)...")
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=60) as ex:
        futures = {ex.submit(tcp_open, ip, PORT): ip for ip in targets}
        for fut in concurrent.futures.as_completed(futures):
            ip = futures[fut]
            try:
                if fut.result():
                    open_hosts.append(ip)
                    print(f"  + {ip}:{PORT} open")
            except Exception:
                pass
    print(f"  scan done in {time.time()-t0:.1f}s")
    return sorted(open_hosts, key=lambda ip: tuple(int(o) for o in ip.split(".")))


def probe_modbus(ip: str):
    """For each device ID 1..5, try to read P21.00 (status) and a small block."""
    if not HAVE_MODBUS:
        print(f"   [no pymodbus available; skipping Modbus probe]")
        return
    c = ModbusTcpClient(ip, port=PORT, timeout=3)
    if not c.connect():
        print(f"   could not Modbus-connect to {ip}:{PORT}")
        return
    try:
        for dev in (1, 2, 3, 4, 5):
            try:
                r = c.read_holding_registers(address=P21_00, count=1, device_id=dev)
                if r.isError():
                    print(f"   dev_id={dev}: error {r}")
                else:
                    val = r.registers[0]
                    note = " (1=ready, 2=running)" if val in (1, 2) else ""
                    print(f"   dev_id={dev}: P21.00 = {val}{note}  <-- LOOKS LIVE")
                    # Also try a short block to identify drive (P21.00..P21.08)
                    rb = c.read_holding_registers(address=P21_00, count=9, device_id=dev)
                    if not rb.isError():
                        print(f"     P21.00..P21.08 = {rb.registers}")
            except Exception as e:
                print(f"   dev_id={dev}: raise {type(e).__name__}: {e}")
    finally:
        c.close()


def main():
    print("=" * 64)
    print(" Modbus TCP recon — is the SV-X3E directly reachable on the LAN?")
    print("=" * 64)
    print()
    laptop = my_lan_ip()
    print(f"Laptop IP : {laptop}")
    print(f"Known HMI : {HMI_IP}")
    print()

    open_hosts = scan_subnet()
    print()
    if not open_hosts:
        print("No hosts on the /24 responded to TCP/502.")
        print("Drive probably NOT on Modbus TCP. CN3 architecture stays.")
        return 0

    print(f"Hosts open on port 502: {open_hosts}")
    print()

    for ip in open_hosts:
        if ip == laptop:
            print(f"-- {ip} (this laptop, skip) --")
            continue
        if ip == HMI_IP:
            print(f"-- {ip} (HMI itself; checking but not the target) --")
        else:
            print(f"-- {ip} --")
        probe_modbus(ip)
        print()

    print("Done.")


if __name__ == "__main__":
    raise SystemExit(main())
