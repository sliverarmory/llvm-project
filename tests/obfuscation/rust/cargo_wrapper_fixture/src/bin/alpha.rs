#[no_mangle]
#[inline(never)]
pub extern "C" fn wrapper_probe(value: u32) -> u32 {
    value.wrapping_add(7)
}

fn main() {
    println!("{}", wrapper_probe(std::hint::black_box(8)));
    println!("{}", std::env::args().skip(1).collect::<Vec<_>>().join("|"));
    #[cfg(wrapper_forwarded)]
    println!("rustc-forwarded");
}

#[test]
fn forwarding_test() {
    assert_eq!(wrapper_probe(std::hint::black_box(8)), 15);
    println!("wrapper-test-forwarding");
}
