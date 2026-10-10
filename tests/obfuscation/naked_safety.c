__attribute__((naked, noinline)) int naked_target(void) {
#if defined(__aarch64__)
  __asm__("mov w0, #42\nret");
#elif defined(__x86_64__)
  __asm__("movl $42, %eax\nret");
#else
#error This fixture supports the packaged AArch64 and x86_64 targets.
#endif
}

__attribute__((noinline)) int ordinary_target(int x) { return x + 1; }

int main(void) {
  return naked_target() != 42 || ordinary_target(41) != 42;
}
