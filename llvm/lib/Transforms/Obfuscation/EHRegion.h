//===- EHRegion.h - Normal-region selection for obfuscation ----*- C++ -*-===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#ifndef LLVM_LIB_TRANSFORMS_OBFUSCATION_EHREGION_H
#define LLVM_LIB_TRANSFORMS_OBFUSCATION_EHREGION_H

#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/IR/CFG.h"
#include "llvm/IR/EHPersonalities.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/Instructions.h"

namespace llvm::obfuscation {

inline bool hasExceptionalControlFlow(const Function &F) {
  for (const BasicBlock &BB : F)
    if (BB.isEHPad() || isa<InvokeInst>(BB.getTerminator()))
      return true;
  return false;
}

/// Mark EH pads, funclet bodies, and cleanup continuations. The remaining
/// blocks are in the ordinary control-flow region.
inline void collectProtectedEHBlocks(Function &F,
                                     SmallPtrSetImpl<BasicBlock *> &Protected) {
  if (!hasExceptionalControlFlow(F))
    return;

  if (F.hasPersonalityFn() &&
      isScopedEHPersonality(classifyEHPersonality(F.getPersonalityFn()))) {
    // Windows MSVC funclet bodies are not necessarily EH pads themselves.
    // Color them and accept only blocks with the single root-function color.
    auto Colors = colorEHFunclets(F);
    BasicBlock *Root = &F.getEntryBlock();
    for (BasicBlock &BB : F) {
      auto It = Colors.find(&BB);
      if (BB.isEHPad() || It == Colors.end() || It->second.size() != 1 ||
          It->second.front() != Root)
        Protected.insert(&BB);
    }
    return;
  }

  // For landingpad-style EH, follow only ordinary edges from the entry.
  // The normal successor of an invoke remains eligible. Independently
  // follow edges out of every EH pad: a cleanup continuation stays protected
  // even when it rejoins a block also reachable by normal control flow.
  SmallPtrSet<BasicBlock *, 32> NormallyReachable;
  SmallVector<BasicBlock *, 32> Worklist{&F.getEntryBlock()};
  while (!Worklist.empty()) {
    BasicBlock *BB = Worklist.pop_back_val();
    if (BB->isEHPad() || !NormallyReachable.insert(BB).second)
      continue;
    if (auto *Invoke = dyn_cast<InvokeInst>(BB->getTerminator())) {
      Worklist.push_back(Invoke->getNormalDest());
      continue;
    }
    for (BasicBlock *Succ : successors(BB))
      if (!Succ->isEHPad())
        Worklist.push_back(Succ);
  }
  SmallPtrSet<BasicBlock *, 32> CleanupReachable;
  for (BasicBlock &BB : F)
    if (BB.isEHPad())
      Worklist.push_back(&BB);
  while (!Worklist.empty()) {
    BasicBlock *BB = Worklist.pop_back_val();
    if (!CleanupReachable.insert(BB).second)
      continue;
    for (BasicBlock *Succ : successors(BB))
      Worklist.push_back(Succ);
  }
  for (BasicBlock &BB : F)
    if (!NormallyReachable.contains(&BB) || CleanupReachable.contains(&BB))
      Protected.insert(&BB);
}

} // namespace llvm::obfuscation

#endif // LLVM_LIB_TRANSFORMS_OBFUSCATION_EHREGION_H
