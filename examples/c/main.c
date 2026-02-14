#include <stdint.h>
#include <stdio.h>
#include <string.h>

__attribute__((annotate("sub"), annotate("split"), annotate("bcf"),
               annotate("fla"), noinline))
static int compute_value(int seed, const char *tag) {
  int acc = seed;
  size_t n = strlen(tag);

  for (int i = 0; i < 6; ++i) {
    if (((acc + i) & 1) == 0)
      acc += (i * 3) ^ (seed + (int)n);
    else
      acc -= (seed - i);
  }

  switch ((acc ^ (int)tag[0]) & 3) {
  case 0:
    acc += 17;
    break;
  case 1:
    acc ^= 0x33;
    break;
  case 2:
    acc -= 9;
    break;
  default:
    acc += 5;
    break;
  }

  return acc + (int)n;
}

int main(int argc, char **argv) {
  const char *secret = "C_EXAMPLE_SECRET_MARKER";
  int seed = argc * 11;
  if (argv && argv[0] && argv[0][0] != '\0')
    seed += (unsigned char)argv[0][0];

  int value = compute_value(seed, secret);
  printf("c-example result=%d text=%s\n", value, secret);
  return 0;
}

