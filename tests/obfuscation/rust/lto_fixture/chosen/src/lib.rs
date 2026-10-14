use std::hint::black_box;

static mut STATE: u64 = 7;

// Keep a second protected symbol in a separate module. With four codegen
// units this must occupy a different selected-crate CGU, so lto=false tests
// actually exercise local ThinLTO on protected code instead of only on the
// unselected application crate.
mod local_thin {
    use std::hint::black_box;

    #[unsafe(no_mangle)]
    #[inline(never)]
    pub extern "C" fn lto_local_thin_probe(x: u64) -> u64 {
        let value = black_box(x.wrapping_add(0x1357));
        if value & 1 == 0 {
            black_box(value.wrapping_mul(11).wrapping_add(x))
        } else {
            black_box(value.wrapping_mul(13).wrapping_sub(x))
        }
    }
}

pub use local_thin::lto_local_thin_probe;

#[unsafe(no_mangle)]
#[inline(never)]
pub extern "C" fn lto_chosen(x: u64, y: u64) -> u64 {
    // The result is deliberately observed without changing the existing
    // output oracle. The linked executable must retain both codegen units.
    black_box(lto_local_thin_probe(x));
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
