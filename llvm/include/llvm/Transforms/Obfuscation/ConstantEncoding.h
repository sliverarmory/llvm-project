//===- ConstantEncoding.h - Selected integer constant encoding -*- C++ -*-===//

#ifndef LLVM_TRANSFORMS_OBFUSCATION_CONSTANTENCODING_H
#define LLVM_TRANSFORMS_OBFUSCATION_CONSTANTENCODING_H

#include "llvm/IR/PassManager.h"

namespace llvm {

class ConstantEncodingPass : public PassInfoMixin<ConstantEncodingPass> {
public:
  explicit ConstantEncodingPass(bool Flag = false) : Flag(Flag) {}
  PreservedAnalyses run(Module &M, ModuleAnalysisManager &AM);

private:
  bool Flag = false;
};

} // namespace llvm

#endif // LLVM_TRANSFORMS_OBFUSCATION_CONSTANTENCODING_H
