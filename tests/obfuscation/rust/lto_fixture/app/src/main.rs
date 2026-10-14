use std::hint::black_box;

fn main() {
    for x in 1..=4 {
        let chosen = rust_obf_lto_chosen::lto_chosen(black_box(x), 29);
        let plain = rust_obf_lto_plain::lto_plain(black_box(x), 29);
        println!("{chosen}:{plain}");
    }
    println!("{}", rust_obf_lto_chosen::selected_text());
    println!("{}", rust_obf_lto_chosen::lto_global(5));
}
