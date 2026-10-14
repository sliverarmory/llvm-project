static MEMBER_TEXT: [u8; 28] = *b"m2-member-text-secret-75b13a";
const INLINE_TEXT: &str = "m2-inline-secret-04b1a8";

#[no_mangle]
#[inline(never)]
pub extern "C" fn member_value(a: u32, b: u32) -> u32 {
    // Keep a Rust-style, non-NUL string allocation reachable from the
    // exported function without changing the arithmetic result.
    let text = std::hint::black_box(&MEMBER_TEXT[..]);
    let inline = std::hint::black_box(INLINE_TEXT).as_bytes();
    if text.first().copied() != Some(b'm') || inline.first().copied() != Some(b'm') {
        std::process::abort();
    }
    let base = a.wrapping_add(0x5a17);
    if b & 1 == 0 {
        std::hint::black_box(base).wrapping_add(b)
    } else {
        base.wrapping_add(std::hint::black_box(b))
    }
}
