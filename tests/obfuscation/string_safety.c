// Keep cross-target object tests independent of platform SDK headers.
typedef __SIZE_TYPE__ size_t;
extern int puts(const char *);
extern size_t strlen(const char *);

#if defined(__APPLE__)
#define READ_ONLY_SECTION "__TEXT,__obf_string"
#elif defined(_WIN32)
#define READ_ONLY_SECTION ".rdata$obf_string"
#else
#define READ_ONLY_SECTION ".rodata.obf_string"
#endif

static const char section_message[]
    __attribute__((section(READ_ONLY_SECTION))) = "section-read-only";
static const char early_message[] = "early-constructor-data";
static const char *const early_pointer = early_message;
static const char late_message[] = "late-constructor-data";
// An internal array is eligible for encoding on every target. Inline literals
// may instead be emitted as linker-coalesced COMDAT data (notably on Windows).
static const char short_message[] = "obf-short-literal";
static size_t early_size;
static size_t late_size;

// Follow a direct callee and a constant pointer descriptor when checking
// whether startup can read the string before the priority-zero decoder.
__attribute__((noinline)) static size_t early_reader(void) {
  return strlen(early_pointer);
}

__attribute__((constructor(0))) static void early_ctor(void) {
  early_size = early_reader();
}

__attribute__((constructor(101))) static void late_ctor(void) {
  late_size = strlen(late_message);
}

int main(void) {
  if (early_size != sizeof(early_message) - 1 ||
      late_size != sizeof(late_message) - 1 ||
      strlen(section_message) != sizeof(section_message) - 1)
    return 1;
  return puts(short_message) < 0 || puts("pooled-inline-literal") < 0;
}
