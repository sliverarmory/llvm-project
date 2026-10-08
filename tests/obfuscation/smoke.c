#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static const char message[] = "obfuscation-smoke-marker-79e6b1";
volatile uint32_t smoke_sink;

// The volatile stores keep both branches in the optimized IR. The arithmetic
// remains dependent on runtime inputs so every transform has work to do.
__attribute__((noinline)) uint32_t transform_target(uint32_t a, uint32_t b) {
  uint32_t x = (a + b) ^ (a | 0xa5a5u);
  uint32_t y = (a & b) + 11u;

  if (x & 1u) {
    smoke_sink = x;
    x = x * 3u - y;
  } else {
    smoke_sink = y;
    x = x * 2u + y;
  }

  if (b & 2u) {
    smoke_sink = x;
    x ^= y | 7u;
  } else {
    smoke_sink = y;
    x += a ^ 13u;
  }

  return x + smoke_sink;
}

int main(int argc, char **argv) {
  uint32_t a = argc > 1 ? (uint32_t)strtoul(argv[1], NULL, 0) : 17u;
  uint32_t b = argc > 2 ? (uint32_t)strtoul(argv[2], NULL, 0) : 29u;
  printf("%u:%s\n", transform_target(a, b), message);
  return 0;
}
