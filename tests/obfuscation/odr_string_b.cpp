#include "odr_string.hpp"

static const char private_message_b[] = "cross-tu-private-b-marker";

extern "C" const char *odr_b() { return shared_message; }
extern "C" const char *ordinary_b() { return private_message_b; }
