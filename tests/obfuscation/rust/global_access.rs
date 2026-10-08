//! A minimal private scalar-global witness.  The current global-access pass
//! skips functions with an EH personality, so this is intentionally separate
//! from the branch-heavy, black_box-using Rust program in `direct.rs`.

static mut STATE: u32 = 11;

#[no_mangle]
#[inline(never)]
pub extern "C" fn global_target(x: u32) -> u32 {
    let before = unsafe { STATE };
    let after = before.wrapping_add(x);
    unsafe { STATE = after };
    after ^ before
}

fn main() {
    let first = global_target(17);
    let second = global_target(29);
    println!("{first}:{second}");
}
