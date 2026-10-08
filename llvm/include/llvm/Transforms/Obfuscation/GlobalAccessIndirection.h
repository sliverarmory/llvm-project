//===- GlobalAccessIndirection.h - Selective global access indirection ---===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#ifndef LLVM_TRANSFORMS_OBFUSCATION_GLOBALACCESSINDIRECTION_H
#define LLVM_TRANSFORMS_OBFUSCATION_GLOBALACCESSINDIRECTION_H

#include "llvm/IR/PassManager.h"

namespace llvm {

class GlobalAccessIndirectionPass
    : public PassInfoMixin<GlobalAccessIndirectionPass> {
public:
  explicit GlobalAccessIndirectionPass(bool Flag = false) : Flag(Flag) {}

  PreservedAnalyses run(Module &M, ModuleAnalysisManager &AM);

private:
  bool Flag;
};

} // namespace llvm

#endif // LLVM_TRANSFORMS_OBFUSCATION_GLOBALACCESSINDIRECTION_H
