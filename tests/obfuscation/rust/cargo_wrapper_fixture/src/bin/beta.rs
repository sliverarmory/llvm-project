#[no_mangle]
#[inline(never)]
pub extern "C" fn wrapper_probe(_value: u32) -> u32 {
    // Same selected symbol in a different target, with no eligible arithmetic.
    4
}

fn main() {
    std::hint::black_box(wrapper_probe(8));
}
