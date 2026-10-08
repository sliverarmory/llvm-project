#include <stdint.h>
#include <stdio.h>

#ifdef _WIN32
#include <windows.h>
#else
#include <dlfcn.h>
#endif

typedef uint32_t (*probe_fn)(uint32_t, uint32_t);

int main(int argc, char **argv) {
  if (argc != 2)
    return 2;
#ifdef _WIN32
  HMODULE library = LoadLibraryA(argv[1]);
  if (!library) {
    fputs("LoadLibrary failed\n", stderr);
    return 3;
  }
  probe_fn probe = (probe_fn)(void *)GetProcAddress(library, "accept_cdylib_probe");
#else
  void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
  if (!library) {
    fprintf(stderr, "dlopen failed: %s\n", dlerror());
    return 3;
  }
  probe_fn probe = (probe_fn)dlsym(library, "accept_cdylib_probe");
#endif
  if (!probe) {
    fputs("missing accept_cdylib_probe\n", stderr);
    return 4;
  }
  uint32_t value = probe(7, 9);
  printf("%u\n", value);
#ifdef _WIN32
  FreeLibrary(library);
#else
  dlclose(library);
#endif
  return value == 23079 ? 0 : 5;
}
