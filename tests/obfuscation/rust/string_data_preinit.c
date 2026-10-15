#include <stdint.h>
#include <stdio.h>

extern uint8_t OBSERVED;
extern uint8_t observed(void);
extern uint8_t late_first_byte(void);

uint8_t read_first_byte(const uint8_t *ptr) {
  return *(const volatile uint8_t *)ptr;
}

int main(void) {
  uint8_t stored = OBSERVED;
  uint8_t loaded = observed();
  uint8_t later = late_first_byte();
  printf("preinit=%u observed=%u later=%u\n", stored, loaded, later);
  return stored == 'r' && loaded == 'r' && later == 'r' ? 0 : 1;
}
