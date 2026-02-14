#import <Foundation/Foundation.h>
#include <stdio.h>
#include <string.h>

@interface Worker : NSObject
- (int)processSeed:(int)seed marker:(const char *)marker;
@end

__attribute__((annotate("sub"), annotate("split"), annotate("bcf"),
               annotate("fla"), noinline))
static int obfuscatedCompute(int seed, const char *marker) {
  int acc = seed + (int)strlen(marker);
  for (int i = 0; i < 5; ++i) {
    if (((acc + i) & 1) == 0)
      acc += (i * 7) ^ seed;
    else
      acc -= (seed - i);
  }

  switch ((acc ^ (int)marker[0]) & 3) {
  case 0:
    acc += 3;
    break;
  case 1:
    acc ^= 0x2f;
    break;
  case 2:
    acc -= 13;
    break;
  default:
    acc += 9;
    break;
  }

  return acc;
}

@implementation Worker
- (int)processSeed:(int)seed marker:(const char *)marker {
  return obfuscatedCompute(seed, marker);
}
@end

int main(int argc, char **argv) {
  @autoreleasepool {
    Worker *worker = [Worker new];
    const char *secret = "OBJC_EXAMPLE_SECRET_MARKER";

    int seed = argc * 23;
    if (argv && argv[0] && argv[0][0] != '\0')
      seed += (unsigned char)argv[0][0];

    int value = [worker processSeed:seed marker:secret];
    printf("objc-example result=%d text=%s\n", value, secret);
  }

  return 0;
}
