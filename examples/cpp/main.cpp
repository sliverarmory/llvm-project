#include <cstdio>
#include <cstring>

__attribute__((annotate("sub"), annotate("split"), annotate("bcf"),
               annotate("fla"), noinline))
static int mix_seed(int seed, const char *s) {
  size_t n = std::strlen(s);
  int acc = seed + static_cast<int>(n);
  for (size_t i = 0; i < n; ++i) {
    int ch = static_cast<unsigned char>(s[i]);
    if ((i & 1) == 0)
      acc += (ch ^ static_cast<int>(i + 13));
    else
      acc -= (ch ^ static_cast<int>(seed & 0xff));
  }

  switch (acc & 3) {
  case 0:
    acc += 19;
    break;
  case 1:
    acc ^= 0x5a;
    break;
  case 2:
    acc -= 7;
    break;
  default:
    acc += 11;
    break;
  }

  return acc;
}

int main(int argc, char **argv) {
  const char *secret = "CPP_EXAMPLE_SECRET_MARKER";
  int seed = argc * 17;
  if (argv && argv[0] && argv[0][0] != '\0')
    seed += static_cast<unsigned char>(argv[0][0]);

  int value = mix_seed(seed, secret);
  std::printf("cpp-example result=%d text=%s\n", value, secret);
  return 0;
}
