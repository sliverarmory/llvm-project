#include <stdint.h>
#include <stdio.h>

extern uint32_t accept_staticlib_probe(uint32_t, uint32_t);

int main(void) {
  uint32_t value = accept_staticlib_probe(7, 9);
  printf("%u\n", value);
  return value == 23079 ? 0 : 5;
}
