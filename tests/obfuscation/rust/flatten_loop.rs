// The optimized return block precedes the loop body in Rust 1.99 LLVM IR.
// Full-function flattening must dispatch to the entry's successor, not the
// first block by textual order.

#[unsafe(no_mangle)]
#[inline(never)]
pub extern "C" fn flatten_loop(mut x: u64) -> u64 {
    for j in 0..31_u64 {
        x = x.rotate_left(((j % 29) + 1) as u32).wrapping_add(0x5a17);
        if x & 1 == 0 {
            x = x.wrapping_mul(7);
        } else {
            x = x.wrapping_mul(9);
        }
    }
    x
}

fn main() {
    for input in [1_u64, 2_u64, u64::MAX] {
        println!("{}", flatten_loop(std::hint::black_box(input)));
    }
}
