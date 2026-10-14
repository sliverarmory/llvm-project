#include <stddef.h>
#include <stdint.h>

extern const uint8_t *string_data_library(size_t *length);

int main(void) {
  // Keep the expected bytes out of the C object. The linked executable's
  // plaintext witness must come from the Rust staticlib alone.
  static const volatile uint8_t expected_xor[] = {
      200, 150, 136, 195, 195, 204, 136, 201, 204, 199, 215, 196,
      215, 220, 136, 165, 91, 37,
  };
  size_t length = 0;
  const uint8_t *actual = string_data_library(&length);
  if (!actual || length != sizeof(expected_xor))
    return 1;
  for (size_t i = 0; i < length; ++i)
    if (actual[i] != (uint8_t)(expected_xor[i] ^ 0xa5U))
      return 1;
  return 0;
}
