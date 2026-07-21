# What mortnet teaches us about Mort

Building a network stack in a language you also wrote means every milestone
stress-tests the language. This file collects what each milestone revealed —
it doubles as a wishlist for the next Mort version.

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
