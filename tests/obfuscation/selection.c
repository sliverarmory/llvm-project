#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static const char secret_a[] = "selected-secret-alpha-29fdb7";
static const char secret_b[] = "selected-secret-beta-68a5c1";
__attribute__((weak)) const char weak_message[] = "weak-public-message-0e42c8";

#define BODY                                                                   \
  uint32_t x = ((a + b) * 3u) ^ (a | b);                                      \
  return x + (a & b)

__attribute__((noinline)) uint32_t selected(uint32_t a, uint32_t b) {
  BODY;
}

__attribute__((noinline)) uint32_t neighbor(uint32_t a, uint32_t b) {
  BODY;
}

__attribute__((noinline, annotate("sub")))
uint32_t annotated(uint32_t a, uint32_t b) {
  BODY;
}

__attribute__((noinline, annotate("nosub")))
uint32_t vetoed(uint32_t a, uint32_t b) {
  BODY;
}

int main(int argc, char **argv) {
  uint32_t a = argc > 1 ? (uint32_t)strtoul(argv[1], NULL, 0) : 17u;
  uint32_t b = argc > 2 ? (uint32_t)strtoul(argv[2], NULL, 0) : 29u;
  uint32_t sum = selected(a, b) + neighbor(a, b) + annotated(a, b) +
                 vetoed(a, b);
  printf("%u:%s:%s:%s\n", sum, secret_a, secret_b, weak_message);
  return 0;
}
