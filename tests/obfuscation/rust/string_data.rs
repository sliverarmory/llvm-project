//! Exact-byte Rust string and FFI witness for the LLVM byte-array pass.

use std::hint::black_box;
use std::io::{self, Write};

static STATIC_TEXT: &str = "m3-static-ø-δ";
const CONST_TEXT: &str = "m3-const-漢字";
static STATIC_BYTES: &[u8] = b"m3-bytes-\0\xff\x80";
static FFI_BYTES: &[u8] = b"m3-ffi-\0\xfe";

#[unsafe(no_mangle)]
pub extern "C" fn string_data_ffi(length: *mut usize) -> *const u8 {
    if !length.is_null() {
        // SAFETY: A non-null caller-provided pointer must designate a writable usize.
        unsafe { *length = FFI_BYTES.len() };
    }
    FFI_BYTES.as_ptr()
}

fn write_chunk(out: &mut impl Write, bytes: &[u8]) -> io::Result<()> {
    out.write_all(&(bytes.len() as u32).to_le_bytes())?;
    out.write_all(bytes)
}

fn main() -> io::Result<()> {
    let stdout = io::stdout();
    let mut out = stdout.lock();
    write_chunk(&mut out, black_box("m3-direct-secret-β\0tail").as_bytes())?;
    write_chunk(&mut out, STATIC_TEXT.as_bytes())?;
    write_chunk(&mut out, CONST_TEXT.as_bytes())?;
    write_chunk(&mut out, STATIC_BYTES)?;
    let mut ffi_length = 0;
    let ffi_data = string_data_ffi(&mut ffi_length);
    // SAFETY: string_data_ffi returns a pointer to a static array of this length.
    let ffi_bytes = unsafe { std::slice::from_raw_parts(ffi_data, ffi_length) };
    write_chunk(&mut out, ffi_bytes)
}
