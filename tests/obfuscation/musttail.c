// LLVM requires a musttail call to remain immediately before its return.
static volatile int increment = 7;
__attribute__((noinline)) static int callee(int value) {
  return value + increment;
}

__attribute__((noinline)) static int tail_wrapper(int value) {
  [[clang::musttail]] return callee(value);
}

int main(void) { return tail_wrapper(5) == 12 ? 0 : 1; }
