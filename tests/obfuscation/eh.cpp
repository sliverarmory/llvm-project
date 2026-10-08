// Bogus control flow must not change exception handling edges or landing pads.
extern void may_throw();

int guarded(int value) {
  try {
    may_throw();
    return value + 1;
  } catch (...) {
    return -1;
  }
}
