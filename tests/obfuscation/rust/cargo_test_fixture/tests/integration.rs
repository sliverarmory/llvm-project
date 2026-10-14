#[test]
fn integration_gate() {
    assert_eq!(cargo_test_gate::cargo_test_probe(std::hint::black_box(7)), 42);
    println!("integration-gate-executed");
}
