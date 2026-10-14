fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-env-changed=RUST_OBF_TEST_EXPORT_SYMBOL");
    if cfg!(windows) {
        println!("cargo:rustc-link-arg-bin=rust-obf-app=/EXPORT:member_value");
        if let Ok(symbol) = std::env::var("RUST_OBF_TEST_EXPORT_SYMBOL") {
            assert!(!symbol.is_empty() && symbol.chars().all(|ch| {
                ch.is_ascii_alphanumeric() || "_.$@?".contains(ch)
            }));
            println!("cargo:rustc-link-arg-bin=rust-obf-app=/EXPORT:{symbol}");
        }
    }
}
