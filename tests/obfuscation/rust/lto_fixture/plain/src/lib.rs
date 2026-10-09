use std::hint::black_box;

#[unsafe(no_mangle)]
#[inline(never)]
pub extern "C" fn lto_plain(x: u64, y: u64) -> u64 {
    // Keep this unselected witness distinct from lto_chosen under fat LTO.
    // Otherwise whole-program identical-function folding can alias the two.
    let value = x.wrapping_add(0x5a17).wrapping_add(black_box(0_u64));
    if value & 1 == 0 {
        black_box(value.wrapping_mul(9).wrapping_add(y))
    } else {
        black_box(value.wrapping_mul(7).wrapping_sub(y))
    }
}
