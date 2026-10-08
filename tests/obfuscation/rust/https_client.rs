//! Standalone Rust HTTPS client for milestone-0 E2E.  Direct libcurl FFI keeps
//! this fixture buildable with rustc alone (no Cargo registry dependencies).
//! The regression runner serves deterministic local HTTPS; `--live-https`
//! additionally fetches https://example.com/.

use std::env;
use std::ffi::{c_char, c_int, c_long, c_void, CStr, CString};
use std::slice;

const CURL_GLOBAL_ALL: c_long = 3;
const CURLOPT_WRITEDATA: c_int = 10001;
const CURLOPT_URL: c_int = 10002;
const CURLOPT_WRITEFUNCTION: c_int = 20011;
const CURLOPT_FOLLOWLOCATION: c_int = 52;
const CURLOPT_FAILONERROR: c_int = 45;
const CURLOPT_SSL_VERIFYPEER: c_int = 64;
const CURLOPT_CAINFO: c_int = 10065;
const CURLOPT_SSL_VERIFYHOST: c_int = 81;
const CURLOPT_TIMEOUT_MS: c_int = 155;
// CURLOPT_PROTOCOLS_STR and CURLOPT_REDIR_PROTOCOLS_STR require libcurl 7.85+.
const CURLOPT_PROTOCOLS_STR: c_int = 10318;
const CURLOPT_REDIR_PROTOCOLS_STR: c_int = 10319;
const CURLINFO_RESPONSE_CODE: c_int = 0x200002;
const MAX_BODY: usize = 1024 * 1024;

#[link(name = "curl")]
unsafe extern "C" {
    fn curl_global_init(flags: c_long) -> c_int;
    fn curl_global_cleanup();
    fn curl_easy_init() -> *mut c_void;
    fn curl_easy_cleanup(handle: *mut c_void);
    fn curl_easy_setopt(handle: *mut c_void, option: c_int, ...) -> c_int;
    fn curl_easy_perform(handle: *mut c_void) -> c_int;
    fn curl_easy_getinfo(handle: *mut c_void, info: c_int, ...) -> c_int;
    fn curl_easy_strerror(code: c_int) -> *const c_char;
}

extern "C" fn write_body(ptr: *mut c_char, size: usize, count: usize, user: *mut c_void) -> usize {
    let Some(length) = size.checked_mul(count) else { return 0 };
    let body = unsafe { &mut *(user as *mut Vec<u8>) };
    if length > MAX_BODY - body.len() || body.try_reserve(length).is_err() {
        return 0;
    }
    let incoming = unsafe { slice::from_raw_parts(ptr.cast::<u8>(), length) };
    body.extend_from_slice(incoming);
    length
}

fn setopt_long(handle: *mut c_void, option: c_int, value: c_long) -> Result<(), String> {
    let code = unsafe { curl_easy_setopt(handle, option, value) };
    if code == 0 { Ok(()) } else { Err(format!("curl option {option}: error {code}")) }
}

fn setopt_string(handle: *mut c_void, option: c_int, value: &CStr) -> Result<(), String> {
    let code = unsafe { curl_easy_setopt(handle, option, value.as_ptr()) };
    if code == 0 { Ok(()) } else { Err(format!("curl option {option}: error {code}")) }
}

struct CurlHandle(*mut c_void);
impl Drop for CurlHandle {
    fn drop(&mut self) { unsafe { curl_easy_cleanup(self.0) } }
}

struct CurlGlobal;
impl Drop for CurlGlobal {
    fn drop(&mut self) { unsafe { curl_global_cleanup() } }
}

fn fetch(url: &str, ca_file: Option<&str>) -> Result<(c_long, Vec<u8>), String> {
    if !url.starts_with("https://") {
        return Err("only HTTPS URLs are accepted".to_string());
    }
    if unsafe { curl_global_init(CURL_GLOBAL_ALL) } != 0 {
        return Err("curl_global_init failed".to_string());
    }
    let _global = CurlGlobal;
    let handle = CurlHandle(unsafe { curl_easy_init() });
    if handle.0.is_null() { return Err("curl_easy_init failed".to_string()) }

    let url = CString::new(url).map_err(|_| "NUL in URL".to_string())?;
    let https = CString::new("https").unwrap();
    let ca_file = ca_file.map(|s| CString::new(s).map_err(|_| "NUL in CA path".to_string()))
        .transpose()?;
    let mut body = Vec::new();
    setopt_string(handle.0, CURLOPT_URL, &url)?;
    setopt_string(handle.0, CURLOPT_PROTOCOLS_STR, &https)?;
    setopt_string(handle.0, CURLOPT_REDIR_PROTOCOLS_STR, &https)?;
    setopt_long(handle.0, CURLOPT_FOLLOWLOCATION, 1)?;
    setopt_long(handle.0, CURLOPT_FAILONERROR, 1)?;
    setopt_long(handle.0, CURLOPT_SSL_VERIFYPEER, 1)?;
    setopt_long(handle.0, CURLOPT_SSL_VERIFYHOST, 2)?;
    setopt_long(handle.0, CURLOPT_TIMEOUT_MS, 15_000)?;
    if let Some(path) = &ca_file {
        setopt_string(handle.0, CURLOPT_CAINFO, path)?;
    }
    let code = unsafe { curl_easy_setopt(handle.0, CURLOPT_WRITEDATA, &mut body as *mut Vec<u8>) };
    if code != 0 { return Err(format!("curl write-data option: error {code}")) }
    let code = unsafe { curl_easy_setopt(handle.0, CURLOPT_WRITEFUNCTION, write_body as extern "C" fn(_, _, _, _) -> _) };
    if code != 0 { return Err(format!("curl callback option: error {code}")) }

    let code = unsafe { curl_easy_perform(handle.0) };
    if code != 0 {
        let reason = unsafe { CStr::from_ptr(curl_easy_strerror(code)) }.to_string_lossy();
        return Err(format!("HTTPS request failed: {reason}"));
    }
    let mut status: c_long = 0;
    let code = unsafe { curl_easy_getinfo(handle.0, CURLINFO_RESPONSE_CODE, &mut status as *mut c_long) };
    if code != 0 { return Err(format!("curl status lookup: error {code}")) }
    Ok((status, body))
}

#[no_mangle]
#[inline(never)]
pub extern "C" fn summarize_checksum(bytes: *const u8, len: usize) -> u64 {
    let body = unsafe { slice::from_raw_parts(bytes, len) };
    let mut hash = 0xcbf29ce484222325_u64;
    for (i, byte) in body.iter().enumerate() {
        let mixed = (*byte as u64).wrapping_add((i as u64) & 0xff);
        hash ^= mixed;
        hash = hash.wrapping_mul(0x100000001b3);
        if hash & 1 == 0 { hash ^= 0xa5a5a5a5 };
    }
    hash
}

fn summarize(status: c_long, body: &[u8]) -> Result<String, String> {
    let html = std::str::from_utf8(body).map_err(|e| format!("non-UTF-8 HTML: {e}"))?;
    let start = html.find("<title>").ok_or("title missing")? + "<title>".len();
    let end = html[start..].find("</title>").ok_or("title close missing")? + start;
    let title = &html[start..end];
    if title.is_empty() || title.contains('\n') { return Err("invalid title".to_string()) }
    let links = html.match_indices("<a ").count();
    let checksum = summarize_checksum(body.as_ptr(), body.len());
    Ok(format!("status={status};title={title};links={links};bytes={};checksum={checksum:016x}", body.len()))
}

fn main() {
    let mut args = env::args().skip(1);
    let url = args.next().unwrap_or_else(|| "https://example.com/".to_string());
    let ca_file = args.next();
    if args.next().is_some() { panic!("usage: https_client [https-url [CA-file]]") }
    let (status, body) = fetch(&url, ca_file.as_deref()).unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(status, 200, "unexpected HTTP status");
    println!("{}", summarize(status, &body).expect("HTML summary"));
}
