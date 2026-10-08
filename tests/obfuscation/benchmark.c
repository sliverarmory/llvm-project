#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#if defined(_MSC_VER)
#define NOINLINE __declspec(noinline)
#else
#define NOINLINE __attribute__((noinline))
#endif

static const char label[] = "obfuscation-benchmark-v1";

// Keep the arithmetic and branches dependent on runtime values. Unsigned
// arithmetic makes the expected result identical across supported targets.
NOINLINE static uint32_t transform(uint32_t a, uint32_t b) {
  uint32_t x = (a + b) ^ (a | UINT32_C(0xa5a5a5a5));
  uint32_t y = (a & b) + UINT32_C(0x9e3779b9);

  if (x & 1u)
    x = x * 3u - y;
  else
    x = x * 5u + y;

  if (b & 2u)
    x ^= y | 7u;
  else
    x += a ^ 13u;

  unsigned shift = (unsigned)(a & 7u) + 1u;
  return ((x << shift) | (x >> (32u - shift))) ^ y;
}

int main(int argc, char **argv) {
  if (argc != 2) {
    fprintf(stderr, "usage: %s iterations\n", argv[0]);
    return 2;
  }

  errno = 0;
  char *end = NULL;
  unsigned long long count = strtoull(argv[1], &end, 10);
  if (errno || end == argv[1] || *end || count == 0 || count > UINT32_MAX) {
    fputs("iterations must be an integer from 1 to 4294967295\n", stderr);
    return 2;
  }

  uint32_t state = UINT32_C(0x6d2b79f5);
  uint32_t checksum = UINT32_C(0x13579bdf);
  for (uint32_t i = 0; i < (uint32_t)count; ++i) {
    state ^= state << 13;
    state ^= state >> 17;
    state ^= state << 5;
    checksum ^= transform(state, i + checksum);
    checksum = (checksum << 1) | (checksum >> 31);
  }

  printf("%s:%08" PRIx32 "\n", label, checksum);
  return 0;
}
