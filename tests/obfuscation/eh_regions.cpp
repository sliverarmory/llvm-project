// Cross-target EH IR fixture for ordinary branches adjoining unwind funclets.
// No C++ runtime headers are needed for the Windows MSVC IR-only check.
extern void may_throw();
extern volatile int branch_trace;

int guarded_eh(int value) {
  int base;
  if (value & 1) {
    branch_trace = 3;
    base = value * 3;
  } else {
    branch_trace = 5;
    base = value * 5;
  }

  try {
    may_throw();
    if (value < 0) {
      branch_trace = 7;
      return base - 2;
    }
    return base + 11;
  } catch (...) {
    branch_trace = 13;
    return -base;
  }
}
