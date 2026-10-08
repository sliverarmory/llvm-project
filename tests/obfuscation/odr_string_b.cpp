#include "odr_string.hpp"

extern "C" const char *odr_b() { return shared_message; }
extern "C" const char *ordinary_b() { return "cross-tu-private-b-marker"; }
