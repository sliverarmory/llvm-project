use std::hint::black_box;

#[unsafe(no_mangle)]
#[inline(never)]
pub extern "C" fn lto_plain(x: u64, y: u64) -> u64 {
    let value = x.wrapping_add(0x5a17);
    if value & 1 == 0 {
        black_box(value.wrapping_mul(9).wrapping_add(y))
    } else {
        black_box(value.wrapping_mul(7).wrapping_sub(y))
    }
}
