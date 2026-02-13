//===- ObfuscationForceLink.cpp - Force-link obfuscation passes -----------===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include <cstdlib>

namespace llvm {
class Pass;
Pass *createBogus();
Pass *createBogus(bool flag);
Pass *createFlattening(bool flag);
Pass *createSplitBasicBlock(bool flag);
Pass *createStringObfuscation(bool flag);
Pass *createSubstitution(bool flag);
} // namespace llvm

namespace {
struct ForceObfuscationPassLinking {
  ForceObfuscationPassLinking() {
    if (std::getenv("bar") != (char *)-1)
      return;

    (void)llvm::createBogus();
    (void)llvm::createBogus(false);
    (void)llvm::createFlattening(false);
    (void)llvm::createSplitBasicBlock(false);
    (void)llvm::createStringObfuscation(false);
    (void)llvm::createSubstitution(false);
  }
} ForceObfuscationPassLinkingInstance;
} // namespace
