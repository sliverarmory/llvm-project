//! Unwind and checked-overflow fixture for the control-flow passes.

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicUsize, Ordering};

static DROP_COUNT: AtomicUsize = AtomicUsize::new(0);
static DROP_ORDER: AtomicUsize = AtomicUsize::new(0);
static BRANCH_TRACE: AtomicUsize = AtomicUsize::new(0);

struct Guard(usize);

impl Drop for Guard {
    fn drop(&mut self) {
        let slot = DROP_COUNT.fetch_add(1, Ordering::SeqCst);
        DROP_ORDER.fetch_or(self.0 << (slot * 4), Ordering::SeqCst);
    }
}

#[unsafe(no_mangle)]
#[inline(never)]
pub extern "C-unwind" fn eh_probe(seed: i32, mode: u8) -> i32 {
    let _outer = Guard(2);
    // Different observable atomic operations keep a normal branch before
    // the invokes and cleanup pads at both O0 and O2.
    let base = if seed & 1 == 0 {
        BRANCH_TRACE.fetch_add(3, Ordering::SeqCst);
        seed.wrapping_mul(3)
    } else {
        BRANCH_TRACE.fetch_xor(5, Ordering::SeqCst);
        seed.wrapping_mul(5)
    };
    let _inner = Guard(1);
    if mode == 1 {
        panic!("explicit unwind witness");
    }
    if mode == 2 {
        let max = std::hint::black_box(i32::MAX);
        return max + 1; // -C overflow-checks=yes must unwind through both drops.
    }
    base.wrapping_add(11)
}

fn main() {
    std::panic::set_hook(Box::new(|_| {}));
    if std::env::args().any(|arg| arg == "--uncaught-panic") {
        let _ = eh_probe(6, 1);
        return;
    }

    let cases: &[(i32, u8)] = if cfg!(panic = "abort") {
        &[(6, 0), (7, 0)]
    } else {
        &[(6, 0), (7, 0), (6, 1), (i32::MAX, 2)]
    };
    for &(seed, mode) in cases {
        DROP_COUNT.store(0, Ordering::SeqCst);
        DROP_ORDER.store(0, Ordering::SeqCst);
        BRANCH_TRACE.store(0, Ordering::SeqCst);
        let outcome = catch_unwind(AssertUnwindSafe(|| {
            eh_probe(std::hint::black_box(seed), std::hint::black_box(mode))
        }));
        let value = match outcome {
            Ok(number) => number.to_string(),
            Err(_) => "panic".to_string(),
        };
        println!(
            "{seed}:{mode}:{value}:{}:{}:{}",
            DROP_COUNT.load(Ordering::SeqCst),
            DROP_ORDER.load(Ordering::SeqCst),
            BRANCH_TRACE.load(Ordering::SeqCst),
        );
    }
}
