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

# Concatenated in this order; endian before eth before the driver before main.
SOURCES = [
    os.path.join(ROOT, "net", "endian.mx"),
    os.path.join(ROOT, "net", "eth.mx"),
    os.path.join(ROOT, "glue", "rtl8139.mx"),
    os.path.join(HERE, "net_demo.mx"),
]


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


def build():
    os.makedirs(BUILD, exist_ok=True)
    cc = _zig()

    combined = "".join(open(s, encoding="utf-8").read() + "\n" for s in SOURCES)
    demo_c = os.path.join(BUILD, "net_demo.c")
    with open(demo_c, "w", encoding="utf-8") as fh:
        fh.write(mortc.compile_to_c(combined, freestanding=True))

    c_flags = ["-target", TARGET, "-ffreestanding", "-fno-stack-protector",
               "-fno-pie", "-fno-asynchronous-unwind-tables",
               "-fno-unwind-tables", "-O2"]
    demo_o = os.path.join(BUILD, "net_demo.o")
    boot_o = os.path.join(BUILD, "boot.o")
    subprocess.run([*cc, *c_flags, "-c", demo_c, "-o", demo_o], check=True)
    subprocess.run([*cc, "-target", TARGET, "-fno-pie", "-c",
                    os.path.join(HERE, "boot.s"), "-o", boot_o], check=True)
    subprocess.run([
        *cc, "-target", TARGET, "-nostdlib", "-static", "-no-pie",
        "-Wl,-T," + os.path.join(HERE, "linker.ld"),
        "-Wl,--build-id=none", "-o", ELF, boot_o, demo_o,
    ], check=True)
    print(f"built {os.path.relpath(ELF, ROOT)}")


def _qemu_net_args():
    # An emulated RTL8139 on a user-mode network, with every frame it sends or
    # receives mirrored into a pcap file.
    return [
        "-device", "rtl8139,netdev=n0",
        "-netdev", "user,id=n0",
        "-object", f"filter-dump,id=d0,netdev=n0,file={PCAP}",
    ]


def run():
    build()
    qemu = _find_qemu()
    if not qemu:
        sys.exit("qemu-system-i386 not found — install QEMU to run the demo.")
    subprocess.run([qemu, "-kernel", ELF, *_qemu_net_args()])


def capture():
    build()
    qemu = _find_qemu()
    if not qemu:
        sys.exit("qemu-system-i386 not found — install QEMU to run the demo.")
    if os.path.exists(PCAP):
        os.remove(PCAP)

    print("Booting the demo headless with an RTL8139 + packet dump...")
    proc = subprocess.Popen([qemu, "-display", "none", "-kernel", ELF,
                             *_qemu_net_args()])
    # The kernel transmits within milliseconds of boot; give it a moment, then
    # stop QEMU. (No sleep import games — a short wait via communicate timeout.)
    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    ok = verify_pcap(PCAP)
    return 0 if ok else 1


def verify_pcap(path):
    """Assert our broadcast 'MORTNET' frame is in the capture."""
    if not os.path.exists(path):
        print(f"FAIL: no capture written at {os.path.relpath(path, ROOT)}")
        return False
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 24:
        print("FAIL: capture file is empty (no frames transmitted)")
        return False

    magic = struct.unpack("<I", data[:4])[0]
    le = magic in (0xA1B2C3D4, 0xA1B23C4D)
    endian = "<" if le else ">"
    off = 24  # global pcap header
    frames = 0
    hit = False
    while off + 16 <= len(data):
        _ts, _us, caplen, _orig = struct.unpack(endian + "IIII",
                                                 data[off:off + 16])
        off += 16
        frame = data[off:off + caplen]
        off += caplen
        frames += 1
        if len(frame) >= 14:
            dst = frame[0:6]
            etype = struct.unpack(">H", frame[12:14])[0]
            if dst == b"\xff" * 6 and etype == 0x88B5 and b"MORTNET" in frame:
                hit = True

    if hit:
        print(f"PASS: {frames} frame(s) captured; found the broadcast "
              f"MORTNET frame (EtherType 0x88B5).")
        print(f"      Open it in Wireshark:  {os.path.relpath(path, ROOT)}")
        return True
    print(f"FAIL: {frames} frame(s) captured, but none matched our "
          f"broadcast MORTNET frame.")
    return False


COMMANDS = {"build": build, "run": run, "capture": capture}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd not in COMMANDS:
        sys.exit(f"unknown command {cmd!r}; use one of: {', '.join(COMMANDS)}")
    rc = COMMANDS[cmd]()
    sys.exit(rc or 0)
