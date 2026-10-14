#include "odr_string.hpp"

extern "C" const char exported_message[] = "cross-tu-export-marker";

static const char private_message_a[] = "cross-tu-private-a-marker";

extern "C" const char *odr_a() { return shared_message; }
extern "C" const char *ordinary_a() { return private_message_a; }
