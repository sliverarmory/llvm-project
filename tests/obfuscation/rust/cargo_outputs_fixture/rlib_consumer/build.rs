fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    if cfg!(windows) {
        println!("cargo:rustc-link-arg-bin=obf-output-rlib-consumer=/EXPORT:accept_rlib_probe");
    }
}
