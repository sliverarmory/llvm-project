#include <stddef.h>
#include <stdint.h>
#include <string.h>

extern const uint8_t *string_data_library(size_t *length);

int main(void) {
  static const uint8_t expected[] = {
      109, 51, 45, 102, 102, 105, 45, 108, 105, 98, 114, 97,
      114, 121, 45, 0, 254, 128,
  };
  size_t length = 0;
  const uint8_t *actual = string_data_library(&length);
  return !actual || length != sizeof(expected) ||
         memcmp(actual, expected, sizeof(expected)) != 0;
}
