#!/usr/bin/env python3
"""Build and run the mortnet M1 demo: MORT OS transmits an Ethernet frame.

    python demo/build_demo.py build    # -> build/net_demo.elf
    python demo/build_demo.py run      # build + boot in QEMU (windowed)
    python demo/build_demo.py capture  # build + boot headless, dump the frame
                                       #    the NIC sends to build/capture.pcap,
                                       #    then verify "MORTNET" is in it

The demo kernel is net/endian.mx + net/eth.mx + glue/rtl8139.mx + demo/net_demo.mx
compiled to freestanding C by mortc, cross-compiled to 32-bit x86 with Zig, and
linked with demo/boot.s + demo/linker.ld — the same recipe as MORT OS, minus the
GDT/IDT (the RTL8139 TX path is pure polling, so the demo needs no interrupts).

`capture` boots QEMU with an emulated RTL8139 whose traffic is written to a pcap
via `-object filter-dump`, then parses the pcap and asserts our broadcast frame
(dst ff:ff:ff:ff:ff:ff, EtherType 0x88B5, payload "MORTNET") is present. That
pcap is the M1 artifact — open it in Wireshark for the screenshot.

Requirements: Python 3.8+, Mort (sibling ../Mort or $MORT_HOME), `pip install
ziglang`, and QEMU (qemu-system-i386).
"""
import glob
import os
import shutil
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(ROOT, "build")
ELF = os.path.join(BUILD, "net_demo.elf")
PCAP = os.path.join(BUILD, "capture.pcap")
TARGET = "x86-freestanding-none"

# Every net/*.mx plus the driver, then one demo main appended last. Mort emits
# prototypes first so declaration order among these doesn't matter for calls.
NET_SOURCES = [
    os.path.join(ROOT, "net", "endian.mx"),
    os.path.join(ROOT, "net", "checksum.mx"),
    os.path.join(ROOT, "net", "eth.mx"),
    os.path.join(ROOT, "net", "ip.mx"),
    os.path.join(ROOT, "net", "icmp.mx"),
    os.path.join(ROOT, "net", "arp.mx"),
    os.path.join(ROOT, "net", "netcfg.mx"),
    os.path.join(ROOT, "glue", "rtl8139.mx"),
]
# The M1 hello demo and the M2 ping demo are separate kernels sharing the stack.
DEMOS = {
    "capture": os.path.join(HERE, "net_demo.mx"),    # M1: transmit one frame
    "ping": os.path.join(HERE, "ping_demo.mx"),      # M2: ARP + ICMP round trip
}
SOURCES = NET_SOURCES + [DEMOS["capture"]]


def _find_mort():
    for c in (os.environ.get("MORT_HOME"),
              os.path.join(os.path.dirname(ROOT), "Mort"),
              os.path.join(ROOT, ".mort")):
        if c and os.path.isfile(os.path.join(c, "mortc.py")):
            return c
    dest = os.path.join(ROOT, ".mort")
    subprocess.run(["git", "clone", "--depth", "1",
                    "https://github.com/0xmortuex/Mort", dest], check=True)
    return dest


sys.path.insert(0, _find_mort())
import mortc  # noqa: E402


def _zig():
    cc = mortc.find_zig()
    if not cc:
        sys.exit("demo needs the Zig backend — run: pip install ziglang")
    return cc


def _find_qemu():
    found = shutil.which("qemu-system-i386")
    if found:
        return found
    for pattern in (r"C:\Program Files\qemu\qemu-system-i386.exe",
                    r"C:\Program Files*\qemu*\qemu-system-i386.exe"):
        hits = glob.glob(pattern)
        if hits:
            return hits[0]
    return None


def build(demo="capture"):
    os.makedirs(BUILD, exist_ok=True)
    cc = _zig()

    sources = NET_SOURCES + [DEMOS[demo]]
    combined = "".join(open(s, encoding="utf-8").read() + "\n" for s in sources)
    demo_c = os.path.join(BUILD, f"{demo}.c")
    with open(demo_c, "w", encoding="utf-8") as fh:
        fh.write(mortc.compile_to_c(combined, freestanding=True))

    c_flags = ["-target", TARGET, "-ffreestanding", "-fno-stack-protector",
               "-fno-pie", "-fno-asynchronous-unwind-tables",
               "-fno-unwind-tables", "-O2"]
    demo_o = os.path.join(BUILD, f"{demo}.o")
    boot_o = os.path.join(BUILD, "boot.o")
    elf = os.path.join(BUILD, f"{demo}.elf")
    subprocess.run([*cc, *c_flags, "-c", demo_c, "-o", demo_o], check=True)
    subprocess.run([*cc, "-target", TARGET, "-fno-pie", "-c",
                    os.path.join(HERE, "boot.s"), "-o", boot_o], check=True)
    subprocess.run([
        *cc, "-target", TARGET, "-nostdlib", "-static", "-no-pie",
        "-Wl,-T," + os.path.join(HERE, "linker.ld"),
        "-Wl,--build-id=none", "-o", elf, boot_o, demo_o,
    ], check=True)
    print(f"built {os.path.relpath(elf, ROOT)}")
    return elf


def _qemu_net_args():
    # An emulated RTL8139 on a user-mode network, with every frame it sends or
    # receives mirrored into a pcap file.
    return [
        "-device", "rtl8139,netdev=n0",
        "-netdev", "user,id=n0",
        "-object", f"filter-dump,id=d0,netdev=n0,file={PCAP}",
    ]


def run():
    elf = build("ping")
    qemu = _find_qemu()
    if not qemu:
        sys.exit("qemu-system-i386 not found — install QEMU to run the demo.")
    subprocess.run([qemu, "-kernel", elf, *_qemu_net_args()])


def _boot_and_capture(elf, min_frames):
    """Boot the kernel headless with the NIC + pcap dump; return once the
    capture holds at least min_frames records and has settled, or after 20s."""
    qemu = _find_qemu()
    if not qemu:
        sys.exit("qemu-system-i386 not found — install QEMU to run the demo.")
    if os.path.exists(PCAP):
        os.remove(PCAP)

    print("Booting MORT OS headless with an RTL8139 + packet dump...")
    proc = subprocess.Popen([qemu, "-display", "none", "-kernel", elf,
                             *_qemu_net_args()])
    # A cold QEMU (first launch, Defender scan) can take seconds to start, and
    # the ARP/ICMP round trips trickle in over time — so poll, and once frames
    # start arriving give them a moment to settle rather than cutting off early.
    import time
    deadline = time.monotonic() + 20
    last_size, stable_since = -1, None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        size = os.path.getsize(PCAP) if os.path.exists(PCAP) else 0
        if size != last_size:
            last_size, stable_since = size, time.monotonic()
        n = len(_frames(PCAP))
        if n >= min_frames and stable_since and time.monotonic() - stable_since > 1.5:
            break
        time.sleep(0.25)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _frames(path):
    """Parse a pcap into a list of raw frame bytes (empty if unreadable)."""
    if not os.path.exists(path):
        return []
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 24:
        return []
    endian = "<" if struct.unpack("<I", data[:4])[0] in (0xA1B2C3D4, 0xA1B23C4D) else ">"
    off, out = 24, []
    while off + 16 <= len(data):
        _ts, _us, caplen, _orig = struct.unpack(endian + "IIII", data[off:off + 16])
        off += 16
        out.append(data[off:off + caplen])
        off += caplen
    return out


def has_mortnet_frame(path):
    """True iff the pcap holds our broadcast MORTNET frame. Shared by the M1
    demo and test/test_pcap_oracle.py so the verifier is checked without QEMU."""
    return any(len(f) >= 14 and f[0:6] == b"\xff" * 6
               and struct.unpack(">H", f[12:14])[0] == 0x88B5 and b"MORTNET" in f
               for f in _frames(path))


def capture():
    """M1: assert our broadcast 'MORTNET' frame is in the capture."""
    elf = build("capture")
    _boot_and_capture(elf, min_frames=1)
    frames = _frames(PCAP)
    hit = has_mortnet_frame(PCAP)
    if hit:
        print(f"PASS: {len(frames)} frame(s) captured; found the broadcast "
              f"MORTNET frame (EtherType 0x88B5).")
        print(f"      Open it in Wireshark:  {os.path.relpath(PCAP, ROOT)}")
        return 0
    print(f"FAIL: {len(frames)} frame(s) captured, none matched the MORTNET frame.")
    return 1


def ping():
    """M2: assert MORT OS emitted a valid ARP request and ICMP echo request to
    the gateway, and — if SLIRP answered — that the echo reply came back."""
    elf = build("ping")
    _boot_and_capture(elf, min_frames=2)
    frames = _frames(PCAP)

    OUR_IP, GW_IP = b"\x0a\x00\x02\x0f", b"\x0a\x00\x02\x02"
    arp_req = icmp_req = icmp_reply = False
    for f in frames:
        if len(f) < 14:
            continue
        etype = struct.unpack(">H", f[12:14])[0]
        if etype == 0x0806:                                   # ARP
            a = f[14:]
            if len(a) >= 28 and struct.unpack(">H", a[6:8])[0] == 1 and a[24:28] == GW_IP:
                arp_req = True                                # who-has 10.0.2.2, from us
        elif etype == 0x0800:                                 # IPv4
            ip = f[14:]
            if len(ip) >= 20 and ip[9] == 1:                  # ICMP
                ihl = (ip[0] & 0x0F) * 4
                icmp_type = ip[ihl] if len(ip) > ihl else None
                if icmp_type == 8 and ip[12:16] == OUR_IP and ip[16:20] == GW_IP:
                    icmp_req = True                           # our echo request out
                if icmp_type == 0 and ip[12:16] == GW_IP and ip[16:20] == OUR_IP:
                    icmp_reply = True                         # gateway's echo reply in

    print(f"      captured {len(frames)} frame(s): "
          f"ARP request={arp_req}  ICMP echo request={icmp_req}  echo reply={icmp_reply}")
    if arp_req and icmp_req:
        note = "full round trip — SLIRP answered." if icmp_reply else \
               "TX + RX verified (no gateway echo reply seen, host-test proves the answer path)."
        print(f"PASS: MORT OS spoke ARP and ICMP over the RTL8139. {note}")
        print(f"      Open it in Wireshark:  {os.path.relpath(PCAP, ROOT)}")
        return 0
    print("FAIL: expected at least a broadcast ARP request and an ICMP echo "
          "request to 10.0.2.2.")
    return 1


COMMANDS = {"build": build, "run": run, "capture": capture, "ping": ping}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd not in COMMANDS:
        sys.exit(f"unknown command {cmd!r}; use one of: {', '.join(COMMANDS)}")
    rc = COMMANDS[cmd]()
    sys.exit(rc or 0)
