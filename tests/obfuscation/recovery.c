#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static const char recovery_text[] = "known-prefix:recoverable-string-payload";

__attribute__((noinline)) uint32_t recovery_probe(uint32_t x) {
  return x + UINT32_C(0x6b7d4a91);
}

int main(int argc, char **argv) {
  uint32_t value = argc > 1 ? (uint32_t)strtoul(argv[1], NULL, 0) : 0;
  puts(recovery_text);
  printf("%" PRIu32 "\n", recovery_probe(value));
  return 0;
}
