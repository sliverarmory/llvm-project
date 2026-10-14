//! Direct-rustc milestone-0 witness.  Keep its exported function and private
//! scalar global simple enough to identify in emitted LLVM IR.

use std::env;
use std::hint::black_box;

static mut COUNTER: u32 = 7;

// This NUL-terminated byte array is a probe for the existing C-string pass.
// Ordinary non-NUL Rust `&str` support belongs to roadmap milestone 3.
static C_LITERAL: &[u8] = b"rust-m0-cstring-7e93b1\0";

#[no_mangle]
#[inline(never)]
pub extern "C" fn transform_target(a: u32, b: u32) -> u32 {
    let before = unsafe { COUNTER };
    let mixed = a.wrapping_add(0x5a17) ^ b.rotate_left(3);
    let branch = if mixed & 1 == 0 {
        black_box(mixed.wrapping_mul(3).wrapping_add(b))
    } else {
        black_box(mixed.wrapping_sub(b).rotate_right(1))
    };
    let result = match branch & 3 {
        0 => black_box(branch ^ 0xa5a5),
        1 => black_box(branch.wrapping_add(a)),
        2 => black_box(branch.wrapping_mul(5)),
        _ => black_box(branch ^ b),
    };
    unsafe { COUNTER = before.wrapping_add(result ^ a) };
    result ^ before
}

fn main() {
    let mut args = env::args().skip(1);
    let a = args.next().expect("a").parse::<u32>().expect("u32 a");
    let b = args.next().expect("b").parse::<u32>().expect("u32 b");
    assert!(args.next().is_none(), "expected exactly two integers");

    let first = transform_target(a, b);
    let second = transform_target(b, a);
    let bytes = &C_LITERAL[..C_LITERAL.len() - 1];
    let text = std::str::from_utf8(bytes).expect("ASCII fixture marker");
    println!("{first}:{second}:{text}");
}
