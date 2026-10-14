fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    if cfg!(windows) {
        for symbol in [
            "lto_chosen",
            "lto_local_thin_probe",
            "lto_plain",
            "lto_global",
        ] {
            println!("cargo:rustc-link-arg-bin=rust-obf-lto-app=/EXPORT:{symbol}");
        }
    }
}
