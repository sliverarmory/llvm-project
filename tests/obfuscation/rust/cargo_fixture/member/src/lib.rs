#[no_mangle]
#[inline(never)]
pub extern "C" fn member_value(a: u32, b: u32) -> u32 {
    let base = a.wrapping_add(0x5a17);
    if b & 1 == 0 {
        std::hint::black_box(base).wrapping_add(b)
    } else {
        base.wrapping_add(std::hint::black_box(b))
    }
}
