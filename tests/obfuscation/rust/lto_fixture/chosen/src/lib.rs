use std::hint::black_box;

static mut STATE: u64 = 7;

#[unsafe(no_mangle)]
#[inline(never)]
pub extern "C" fn lto_chosen(x: u64, y: u64) -> u64 {
    let value = x.wrapping_add(0x5a17);
    if value & 1 == 0 {
        black_box(value.wrapping_mul(9).wrapping_add(y))
    } else {
        black_box(value.wrapping_mul(7).wrapping_sub(y))
    }
}

#[inline(never)]
pub fn selected_text() -> &'static str {
    "m5-selected-string-583d29"
}

#[unsafe(no_mangle)]
#[inline(never)]
pub extern "C" fn lto_global(x: u64) -> u64 {
    let current = unsafe { STATE };
    let next = current.wrapping_add(x);
    unsafe { STATE = next };
    next
}
