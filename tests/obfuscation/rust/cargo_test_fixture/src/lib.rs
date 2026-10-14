/// A linked library function used by each kind of Cargo test.
///
/// ```
/// assert_eq!(cargo_test_gate::cargo_test_probe(7), 42);
/// ```
#[no_mangle]
#[inline(never)]
pub extern "C" fn cargo_test_probe(value: u32) -> u32 {
    value.wrapping_mul(5).wrapping_add(7)
}

#[cfg(test)]
mod tests {
    #[test]
    fn unit_gate() {
        assert_eq!(super::cargo_test_probe(std::hint::black_box(7)), 42);
        println!("unit-gate-executed");
    }
}
