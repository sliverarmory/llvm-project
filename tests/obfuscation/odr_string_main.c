extern int strcmp(const char *, const char *);
extern int puts(const char *);

extern const char exported_message[];
extern const char *odr_a(void);
extern const char *odr_b(void);
extern const char *ordinary_a(void);
extern const char *ordinary_b(void);

int main(void) {
  if (strcmp(odr_a(), "cross-tu-odr-marker") ||
      strcmp(odr_b(), "cross-tu-odr-marker") ||
      strcmp(exported_message, "cross-tu-export-marker") ||
      strcmp(ordinary_a(), "cross-tu-private-a-marker") ||
      strcmp(ordinary_b(), "cross-tu-private-b-marker"))
    return 1;
  puts("cross-tu strings passed");
  return 0;
}
