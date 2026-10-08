#![no_std]

#[inline(never)]
fn generic_mix<T: Into<u64> + Copy>(value: T) -> u32 {
    (core::hint::black_box(value.into()) as u32).wrapping_mul(3)
}

#[unsafe(no_mangle)]
#[inline(never)]
pub extern "C" fn accept_rlib_probe(a: u32, b: u32) -> u32 {
    let base = core::hint::black_box(a).wrapping_add(0x5a17);
    let extra = generic_mix::<u32>(1) + generic_mix::<u64>(2);
    if b & 1 == 0 {
        base.wrapping_add(core::hint::black_box(b))
            .wrapping_add(extra)
    } else {
        core::hint::black_box(base)
            .wrapping_add(b)
            .wrapping_add(extra)
    }
}
