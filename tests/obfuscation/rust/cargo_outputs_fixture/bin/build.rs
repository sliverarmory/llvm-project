fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    if cfg!(windows) {
        println!("cargo:rustc-link-arg-bin=obf-output-bin=/EXPORT:accept_bin_probe");
    }
}
