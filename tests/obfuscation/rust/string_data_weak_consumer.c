// Verify that two weak/COMDAT definitions coalesce and retain their bytes.
// The expected array is XOR-coded so it cannot satisfy the final-artifact
// plaintext check on behalf of the LLVM IR definitions.
#include <stddef.h>
#include <stdint.h>

extern uint32_t read_a(uint32_t index);
extern uint32_t read_b(uint32_t index);
extern const unsigned char *weak_address_a(void);
extern const unsigned char *weak_address_b(void);

int main(void) {
  static const unsigned char coded[21] = {
      215, 208, 214, 209, 136, 210, 192, 196, 206, 136, 214,
      205, 196, 215, 192, 193, 136, 146, 145, 199, 151,
  };
  const unsigned char *a = weak_address_a();
  const unsigned char *b = weak_address_b();
  if (a == NULL || a != b)
    return 1;
  for (uint32_t i = 0; i < sizeof(coded); ++i) {
    uint32_t expected = coded[i] ^ 0xa5U;
    if (read_a(i) != expected || read_b(i) != expected || a[i] != expected)
      return 2;
  }
  return 0;
}
