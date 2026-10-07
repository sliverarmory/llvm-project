// Compile this as both Objective-C and Objective-C++ with -sobf. Runtime
// metadata must remain plaintext for class registration and message dispatch.
__attribute__((objc_root_class))
@interface ObfuscationProbe
+ (int)statusForURL:(const char *)url;
@end

#ifdef __APPLE__
__attribute__((objc_root_class))
@interface NSString
- (const char *)UTF8String;
@end

id objc_probe_literal(void) { return @"obf-runtime-literal"; }
#endif

volatile int objc_probe_sink;

@implementation ObfuscationProbe
+ (int)statusForURL:(const char *)url {
  int first = (unsigned char)url[0];
  if (first) {
    objc_probe_sink = first;
    return 200;
  }
  objc_probe_sink = 0;
  return 0;
}
@end

#ifdef __cplusplus
extern "C" id objc_getClass(const char *);
#else
extern id objc_getClass(const char *);
#endif

const char *objc_probe_message(void) { return "obf-user-string-marker"; }

int main(void) {
  if (!objc_getClass("ObfuscationProbe"))
    return 1;
  if ([ObfuscationProbe statusForURL:"https://example.invalid/"] != 200)
    return 2;
#ifdef __APPLE__
  const char *literal = [(NSString *)objc_probe_literal() UTF8String];
  const char *expected = "obf-runtime-literal";
  if (!literal)
    return 4;
  unsigned i = 0;
  for (; expected[i]; ++i)
    if (literal[i] != expected[i])
      return 4;
  if (literal[i])
    return 4;
#endif
  return objc_probe_message()[0] == 'o' ? 0 : 3;
}
