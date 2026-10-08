#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

// Both variables remain mutable across calls. Their similar names catch
// substring matching in the exact global allowlist.
static uint32_t selected_value = UINT32_C(0x19a35e21);
static uint32_t selected_value_peer = UINT32_C(0x754c2a93);
static uint32_t vetoed_value = UINT32_C(0x5fc83ad7);
static uint32_t annotated_value = UINT32_C(0x2187bd4f);

__attribute__((noinline)) uint32_t selected(uint32_t x) {
  uint32_t value = selected_value;
  value = (value ^ x) * UINT32_C(1664525) + UINT32_C(1013904223);
  selected_value = value;
  return value;
}

__attribute__((noinline)) uint32_t peer(uint32_t x) {
  uint32_t value = selected_value_peer;
  value = (value + x) * UINT32_C(22695477) + UINT32_C(1);
  selected_value_peer = value;
  return value;
}

__attribute__((noinline, annotate("nogai"))) uint32_t vetoed(uint32_t x) {
  uint32_t value = vetoed_value;
  value = (value ^ (x + UINT32_C(11))) * UINT32_C(747796405);
  vetoed_value = value;
  return value;
}

__attribute__((noinline, annotate("gai"))) uint32_t annotated(uint32_t x) {
  uint32_t value = annotated_value;
  value = (value + x) ^ UINT32_C(0x61378b2f);
  annotated_value = value;
  return value;
}

int main(int argc, char **argv) {
  uint32_t x = argc > 1 ? (uint32_t)strtoul(argv[1], NULL, 0) : 17u;
  uint32_t y = argc > 2 ? (uint32_t)strtoul(argv[2], NULL, 0) : 29u;
  uint32_t result = 0;
  for (unsigned i = 0; i != 4; ++i) {
    result ^= selected(x + i);
    result += peer(y ^ i);
    result ^= vetoed(x ^ y ^ i);
    result += annotated(y + i);
  }
  printf("%" PRIu32 "\n", result);
  return 0;
}
