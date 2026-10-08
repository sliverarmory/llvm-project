//! A staticlib witness for Rust-to-C byte-slice ownership and exact length.

static FFI_BYTES: &[u8] = b"m3-ffi-library-\0\xfe\x80";

#[unsafe(no_mangle)]
pub extern "C" fn string_data_library(length: *mut usize) -> *const u8 {
    if !length.is_null() {
        // SAFETY: The C caller provides a writable usize when this is non-null.
        unsafe { *length = FFI_BYTES.len() };
    }
    FFI_BYTES.as_ptr()
}
