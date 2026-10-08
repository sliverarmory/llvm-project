//===- OptionParser.h - Obfuscation option validation -----------*- C++ -*-===//

#ifndef LLVM_TRANSFORMS_OBFUSCATION_OPTIONPARSER_H
#define LLVM_TRANSFORMS_OBFUSCATION_OPTIONPARSER_H

#include "llvm/ADT/Twine.h"
#include "llvm/Support/CommandLine.h"
#include <limits>

namespace llvm::obfuscation {

// Validate numeric options while parsing the command line. The passes may be
// skipped for a particular module or function, but an invalid option must
// still fail the compiler invocation.
template <int Min, int Max = std::numeric_limits<int>::max()>
class RangedIntParser : public cl::parser<int> {
  static_assert(Min <= Max);

public:
  explicit RangedIntParser(cl::Option &O) : cl::parser<int>(O) {}

  bool parse(cl::Option &O, StringRef ArgName, StringRef Arg, int &Value) {
    if (cl::parser<int>::parse(O, ArgName, Arg, Value))
      return true;
    if (Value >= Min && Value <= Max)
      return false;
    if constexpr (Max == std::numeric_limits<int>::max())
      return O.error(Twine("value must be >= ") + Twine(Min));
    return O.error(Twine("value must be between ") + Twine(Min) + " and " +
                   Twine(Max));
  }
};

} // namespace llvm::obfuscation

#endif // LLVM_TRANSFORMS_OBFUSCATION_OPTIONPARSER_H
