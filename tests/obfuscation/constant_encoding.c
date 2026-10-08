#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define BODY                                                                   \
  return ((x + UINT32_C(0x6b7d4a91)) ^                                     \
          (y < UINT32_C(0xe32b09a7) ? UINT32_C(7) : UINT32_C(3)))

__attribute__((noinline)) uint32_t selected(uint32_t x, uint32_t y) {
  BODY;
}

__attribute__((noinline)) uint32_t neighbor(uint32_t x, uint32_t y) {
  BODY;
}

__attribute__((noinline, annotate("constenc")))
uint32_t annotated(uint32_t x, uint32_t y) {
  BODY;
}

__attribute__((noinline, annotate("noconstenc")))
uint32_t vetoed(uint32_t x, uint32_t y) {
  BODY;
}

int main(int argc, char **argv) {
  uint32_t x = argc > 1 ? (uint32_t)strtoul(argv[1], NULL, 0) : 17u;
  uint32_t y = argc > 2 ? (uint32_t)strtoul(argv[2], NULL, 0) : 29u;
  uint32_t sum = selected(x, y) + neighbor(x, y) + annotated(x, y) +
                 vetoed(x, y);
  printf("%" PRIu32 "\n", sum);
  return 0;
}
