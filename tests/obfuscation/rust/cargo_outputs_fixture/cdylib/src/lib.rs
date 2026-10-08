#[unsafe(no_mangle)]
#[inline(never)]
pub extern "C" fn accept_cdylib_probe(a: u32, b: u32) -> u32 {
    let base = std::hint::black_box(a).wrapping_add(0x5a17);
    if b & 1 == 0 {
        base.wrapping_add(std::hint::black_box(b))
    } else {
        std::hint::black_box(base).wrapping_add(b)
    }
}
