// A stable, externally named function lets the stage tests select only code
// owned by this crate, even when an LTO link imports Rust sysroot modules.
#[unsafe(no_mangle)]
#[inline(never)]
pub extern "C" fn pipeline_probe(seed: u64) -> u64 {
    let mut value = seed ^ 0x6a09_e667_f3bc_c909;
    for round in 1..=96_u64 {
        value = value
            .rotate_left((round % 31 + 1) as u32)
            .wrapping_mul(0x9e37_79b9_7f4a_7c15);
        if (value ^ round) & 1 == 0 {
            value = value.wrapping_add(round.wrapping_mul(17));
        } else {
            value = value.wrapping_sub(round.wrapping_mul(23));
        }
    }
    value
}

fn main() {
    for seed in [0_u64, 1, 0x1234_5678_9abc_def0, u64::MAX] {
        let result = pipeline_probe(std::hint::black_box(seed));
        println!("{result:016x}");
    }
}
