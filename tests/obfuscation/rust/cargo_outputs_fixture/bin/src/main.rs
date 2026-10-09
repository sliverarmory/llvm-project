use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll, Wake, Waker};

#[unsafe(no_mangle)]
#[inline(never)]
pub extern "C" fn accept_bin_probe(a: u32, b: u32) -> u32 {
    let base = std::hint::black_box(a).wrapping_add(0x5a17);
    if b & 1 == 0 {
        base.wrapping_add(std::hint::black_box(b))
    } else {
        std::hint::black_box(base).wrapping_add(b)
    }
}

#[inline(never)]
fn generic_mix<T: Into<u64> + Copy>(value: T) -> u64 {
    let input = std::hint::black_box(value.into());
    (input.rotate_left(1) ^ 11).wrapping_add(20)
}

#[inline(never)]
fn invoke_closure(callback: &dyn Fn(u64) -> u64, value: u64) -> u64 {
    callback(value)
}

#[inline(never)]
async fn async_mix(value: u64) -> u64 {
    std::future::ready(()).await;
    std::hint::black_box(value).rotate_left(1) ^ 8
}

struct NoopWake;
impl Wake for NoopWake {
    fn wake(self: Arc<Self>) {}
}

#[inline(never)]
fn block_on(mut future: Pin<&mut dyn Future<Output = u64>>) -> u64 {
    let waker = Waker::from(Arc::new(NoopWake));
    let mut context = Context::from_waker(&waker);
    loop {
        if let Poll::Ready(value) = future.as_mut().poll(&mut context) {
            return value;
        }
    }
}

fn main() {
    let generic_value =
        generic_mix(std::hint::black_box(7_u32)) + generic_mix(std::hint::black_box(9_u64));
    let salt = std::hint::black_box(5_u64);
    let closure = move |value: u64| value.rotate_left(1) ^ salt;
    let closure_value = invoke_closure(std::hint::black_box(&closure as &dyn Fn(u64) -> u64), 9);
    let mut future = Box::pin(async_mix(std::hint::black_box(9)));
    let async_value = block_on(std::hint::black_box(future.as_mut()));
    println!(
        "{}:{generic_value}:{closure_value}:{async_value}",
        accept_bin_probe(7, 9)
    );
}
