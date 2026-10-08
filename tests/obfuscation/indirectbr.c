// A computed goto emits indirectbr at -O0. Flattening must leave functions
// with unsupported terminators intact instead of casting them to branches.
__attribute__((noinline)) static int dispatch(int choice) {
  void *targets[] = {&&first, &&second};
  goto *targets[choice & 1];

first:
  return 11;
second:
  return 17;
}

int main(void) {
  return dispatch(0) == 11 && dispatch(1) == 17 ? 0 : 1;
}
