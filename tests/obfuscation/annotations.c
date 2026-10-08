#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#if defined(TEST_SUB)
#define POSITIVE_ANNOTATION "sub"
#define NEGATIVE_ANNOTATION "nosub"
#define MIXED_POSITIVE_ANNOTATION "SuB"
#elif defined(TEST_SPLIT)
#define POSITIVE_ANNOTATION "split"
#define NEGATIVE_ANNOTATION "nosplit"
#define MIXED_POSITIVE_ANNOTATION "SpLiT"
#elif defined(TEST_BCF)
#define POSITIVE_ANNOTATION "bcf"
#define NEGATIVE_ANNOTATION "nobcf"
#define MIXED_POSITIVE_ANNOTATION "BcF"
#elif defined(TEST_FLA)
#define POSITIVE_ANNOTATION "fla"
#define NEGATIVE_ANNOTATION "nofla"
#define MIXED_POSITIVE_ANNOTATION "FlA"
#else
#error Define exactly one TEST_* transform selector
#endif

volatile uint32_t annotation_sink;

#define TARGET_BODY                                                            \
  uint32_t x = (a + b) ^ (a | 0xa5a5u);                                        \
  uint32_t y = (a & b) + 11u;                                                  \
  if (x & 1u) {                                                                \
    annotation_sink = x;                                                       \
    x = x * 3u - y;                                                            \
  } else {                                                                     \
    annotation_sink = y;                                                       \
    x = x * 2u + y;                                                            \
  }                                                                            \
  if (b & 2u) {                                                                \
    annotation_sink = x;                                                       \
    x ^= y | 7u;                                                               \
  } else {                                                                     \
    annotation_sink = y;                                                       \
    x += a ^ 13u;                                                              \
  }                                                                            \
  return x + annotation_sink

__attribute__((noinline, annotate(POSITIVE_ANNOTATION)))
uint32_t positive(uint32_t a, uint32_t b) {
  TARGET_BODY;
}

__attribute__((noinline, annotate(NEGATIVE_ANNOTATION)))
uint32_t negative(uint32_t a, uint32_t b) {
  TARGET_BODY;
}

__attribute__((noinline, annotate("prefix-" POSITIVE_ANNOTATION)))
uint32_t collision_positive(uint32_t a, uint32_t b) {
  TARGET_BODY;
}

__attribute__((noinline, annotate(NEGATIVE_ANNOTATION "-suffix")))
uint32_t collision_negative(uint32_t a, uint32_t b) {
  TARGET_BODY;
}

__attribute__((noinline, annotate(MIXED_POSITIVE_ANNOTATION)))
uint32_t mixed_positive(uint32_t a, uint32_t b) {
  TARGET_BODY;
}

__attribute__((noinline, annotate(POSITIVE_ANNOTATION),
               annotate(NEGATIVE_ANNOTATION)))
uint32_t positive_and_negative(uint32_t a, uint32_t b) {
  TARGET_BODY;
}

int main(int argc, char **argv) {
  uint32_t a = argc > 1 ? (uint32_t)strtoul(argv[1], NULL, 0) : 17u;
  uint32_t b = argc > 2 ? (uint32_t)strtoul(argv[2], NULL, 0) : 29u;
  printf("%u:%u:%u:%u:%u:%u\n", positive(a, b), negative(a, b),
         collision_positive(a, b), collision_negative(a, b),
         mixed_positive(a, b), positive_and_negative(a, b));
  return 0;
}
