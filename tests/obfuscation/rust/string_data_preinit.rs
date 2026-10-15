//! ELF preinit reads must observe plaintext before the string decoder runs.

#![no_std]

unsafe extern "C" {
    fn read_first_byte(ptr: *const u8) -> u8;
}

static EARLY: &str = "rust-preinit-secret";
static LATE: &str = "rust-late-secret";

#[unsafe(no_mangle)]
pub static mut OBSERVED: u8 = 0;

#[unsafe(no_mangle)]
pub extern "C" fn observed() -> u8 {
    unsafe { OBSERVED }
}

extern "C" fn before_ctors() {
    let byte = unsafe { read_first_byte(EARLY.as_ptr()) };
    unsafe { OBSERVED = byte };
}

#[used]
#[unsafe(link_section = ".preinit_array")]
static PREINIT: extern "C" fn() = before_ctors;

#[unsafe(no_mangle)]
pub extern "C" fn late_first_byte() -> u8 {
    unsafe { read_first_byte(LATE.as_ptr()) }
}
