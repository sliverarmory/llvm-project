use rust_obf_macro_host::identity;

fn main() {
    let value = rust_obf_member::member_value(17, 29);
    let mut decimal = itoa::Buffer::new();
    let mut floating = ryu::Buffer::new();
    let checksum = adler2::adler32_slice(b"cargo-obf");
    let output = format!("{}:{}:{}:{}", identity!(value), decimal.format(value),
                         floating.format(1.5), checksum);
    assert_eq!(output, "23109:23109:1.5:292160369");
    println!("{output}");
}
