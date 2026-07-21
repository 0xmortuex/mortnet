# What mortnet teaches us about Mort

Building a network stack in a language you also wrote means every milestone
stress-tests the language. This file collects what each milestone revealed —
it doubles as a wishlist for the next Mort version.

## From M2 (ARP + ICMP)

1. **No modules, no imports — the stack is one giant translation unit.** mortc
   compiles a single `.mx` file, so `build_demo.py` and `test/run_tests.py`
   concatenate every `net/*.mx` (+ the driver, + one demo main) into one file
   before compiling. It works because Mort emits all prototypes first, so call
   order across "modules" doesn't matter — but there's no namespacing, and every
   global (`g_our_ip`, `g_rtl_rxbuf`, ...) shares one flat scope. A real module
   system (or even a documented multi-file compile) is the biggest missing
   ergonomic feature now.

2. **M2 needed no new language features.** After M1 added `inl/outl`, the whole
   IPv4 + ARP + ICMP layer and the RX ring compiled with what Mort already had:
   fixed-width ints, pointers, structs-free byte poking through `u64` addresses,
   and `&&`/`||` in conditions. That's a good sign the core is capable — the
   friction is now ergonomics (modules, `&arr[i]`, `const`), not capability.

3. **`%` and unsigned wrap both behave.** The RX ring cursor uses
   `(off + advance) % 8192` and `(off - 16) as u16` (deliberate wrap to 0xFFF0
   for the card's CAPR quirk); both generated correct C. Handy to have
   confirmed for the sliding-window arithmetic coming in M5 (TCP).

## From M1 (NIC driver)

1. **The language was missing 32-bit port I/O.** Mort v0.7 had `inb/outb`
   and `inw/outw`, but PCI configuration space is addressed through a dword
   register pair on ports `0xCF8`/`0xCFC`, which needs `inl/outl`. Added them
   to the compiler (typechecker + codegen, emitted only when used, 5 new
   tests) — the first time mortnet drove a change to Mort itself. Committed
   upstream in the Mort repo.

2. **Pointer-to-`u32` casts warn but are correct on the 32-bit target.**
   `&g_rtl_txbuf as u32` triggers `-Wpointer-to-int-cast`, exactly like the
   MORT OS ATA driver's `buf as u32`. Harmless: the kernel is 32-bit with
   identity-mapped memory, so a pointer *is* a `u32`. A future `--freestanding`
   host-word-size note or a `ptr_addr()` intrinsic could silence it.

3. **Polled TX needs no interrupts**, which let the demo kernel skip the
   GDT/IDT entirely and use a 30-line boot stub — so the M1 demo is
   self-contained in `demo/` and doesn't fork MORT OS's 1800-line kernel.

## From M0 (foundations)

1. **`&arr[i]` is not addressable.** Mort v0.7 can take the address of a
   variable but not of an array element. mortnet's answer: all buffer APIs
   trade in `u64` addresses (`buf_addr(slot)`), the same idiom the kernel
   uses for hardware memory. A future `&arr[i]` (or slices) would make the
   call sites prettier.

2. **Repeat initializers materialize every element.** `[0; 12800]` emits
   12,800 literal zeros into the generated C (~50 KB of text for the packet
   pool). Harmless but ugly; zero-init elision in codegen (`{0}` or a
   `memset`-free static) is an easy compiler win.

3. **No `const`.** Slot size (1600) and slot count (8) appear as literals
   with comments instead of named compile-time constants. Top-level `let`
   works but is mutable state, not a constant.

4. **Struct layout follows C, padding included.** Verified empirically:
   `struct { used: u8, len: u16, data: [u8; 16] }` puts `data` at offset 4.
   mortnet avoids offset arithmetic into structs regardless — parsing
   happens through the `be16_load`/`be32_load` helpers.

5. **What already worked without a single fight:** fixed-width unsigned
   types with proper wrap semantics, bitwise ops keeping their width
   (`swap16` emits fully-cast C), global arrays as static storage, range
   `for` with typed counters, and `--emit-c` as a zero-dependency
   front-end test. The checksum and endian modules compiled correctly on
   the first try.
