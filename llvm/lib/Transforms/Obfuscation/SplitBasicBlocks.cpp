//===- SplitBasicBlock.cpp - SplitBasicBlokc Obfuscation pass--------------===//
//
//                     The LLVM Compiler Infrastructure
//
// This file is distributed under the University of Illinois Open Source
// License. See LICENSE.TXT for details.
//
//===----------------------------------------------------------------------===//
//
// This file implements the split basic block pass
//
//===----------------------------------------------------------------------===//

#include "llvm/Transforms/Obfuscation/CryptoUtils.h"
#include "llvm/Transforms/Obfuscation/OptionParser.h"
#include "llvm/Transforms/Obfuscation/Split.h"
#include "llvm/Transforms/Obfuscation/Utils.h"
#include <algorithm>
#include <memory>

#define DEBUG_TYPE "split"

using namespace llvm;

// Stats
STATISTIC(Split, "Basicblock splitted");

static cl::opt<int, false, obfuscation::RangedIntParser<2, 10>> SplitNum(
    "split_num", cl::init(2), cl::desc("Split <split_num> time each BB"));

namespace {
struct SplitBasicBlock : public FunctionPass {
  static char ID; // Pass identification, replacement for typeid
  bool flag = false;

  SplitBasicBlock() : FunctionPass(ID) {}
  SplitBasicBlock(bool flag) : FunctionPass(ID), flag(flag) {}

  bool runOnFunction(Function &F) override;
  bool split(Function *f);

  bool containsPHI(BasicBlock *b);
  void shuffle(std::vector<int> &vec);
};
} // namespace

char SplitBasicBlock::ID = 0;
static RegisterPass<SplitBasicBlock> X("splitbbl", "BasicBlock splitting");

Pass *llvm::createSplitBasicBlock(bool flag) {
  return new SplitBasicBlock(flag);
}

PreservedAnalyses SplitBasicBlockPass::run(Function &F,
                                           FunctionAnalysisManager &AM) {
  (void)AM;
  std::unique_ptr<Pass> Legacy(createSplitBasicBlock(Flag));
  bool Changed = static_cast<FunctionPass *>(Legacy.get())->runOnFunction(F);
  return Changed ? PreservedAnalyses::none() : PreservedAnalyses::all();
}

bool SplitBasicBlock::runOnFunction(Function &F) {
  // Guard against invalid values set programmatically after option parsing.
  if (!((SplitNum > 1) && (SplitNum <= 10))) {
    F.getContext().emitError("-split_num must be between 2 and 10");
    return false;
  }

  Function *tmp = &F;

  // Do we obfuscate
  if (toObfuscate(flag, tmp, "split")) {
    return split(tmp);
  }

  return false;
}

bool SplitBasicBlock::split(Function *f) {
  // Splitting can move a convergence entry intrinsic out of the entry block
  // or change a loop token's cycle. Keep convergent control flow intact.
  if (f->isConvergent()) {
    reportObfuscationSkip("split", "function", f->getName(), "convergent");
    return false;
  }
  for (BasicBlock &BB : *f) {
    for (Instruction &I : BB) {
      if (I.getType()->isTokenTy()) {
        reportObfuscationSkip("split", "function", f->getName(), "token");
        return false;
      }
      if (auto *Call = dyn_cast<CallBase>(&I)) {
        if (Call->isConvergent() ||
            Call->countOperandBundlesOfType(LLVMContext::OB_convergencectrl)) {
          reportObfuscationSkip("split", "function", f->getName(),
                                "convergent-call");
          return false;
        }
      }
    }
  }

  std::vector<BasicBlock *> origBB;
  bool Changed = false;
  bool SawPHI = false;
  bool SawMustTail = false;

  // Save all basic blocks
  for (Function::iterator I = f->begin(), IE = f->end(); I != IE; ++I) {
    origBB.push_back(&*I);
  }

  for (std::vector<BasicBlock *>::iterator I = origBB.begin(),
                                           IE = origBB.end();
       I != IE; ++I) {
    BasicBlock *curr = *I;

    // No need to split a 1 inst bb
    // Or ones containing a PHI node or a musttail call.
    if (curr->size() < 2)
      continue;
    if (containsPHI(curr)) {
      SawPHI = true;
      continue;
    }
    if (hasMustTailCall(*curr)) {
      SawMustTail = true;
      continue;
    }

    // Cap each block independently. A small earlier block must not reduce the
    // number of split points selected for later, larger blocks.
    int splitN = std::min<int>(SplitNum, curr->size() - 1);

    // Generate splits point
    std::vector<int> test;
    for (unsigned i = 1; i < curr->size(); ++i) {
      test.push_back(i);
    }

    // Shuffle
    if (test.size() != 1) {
      shuffle(test);
      std::sort(test.begin(), test.begin() + splitN);
    }

    // Split
    BasicBlock::iterator it = curr->begin();
    BasicBlock *toSplit = curr;
    int last = 0;
    for (int i = 0; i < splitN; ++i) {
      for (int j = 0; j < test[i] - last; ++j) {
        ++it;
      }
      last = test[i];
      if (toSplit->size() < 2)
        continue;
      toSplit = toSplit->splitBasicBlock(it, toSplit->getName() + ".split");
      Changed = true;
    }

    ++Split;
  }

  if (!Changed) {
    StringRef Reason = "no-eligible-blocks";
    if (SawMustTail)
      Reason = "musttail";
    else if (SawPHI)
      Reason = "phi";
    reportObfuscationSkip("split", "function", f->getName(), Reason);
  }
  return Changed;
}

bool SplitBasicBlock::containsPHI(BasicBlock *b) {
  for (BasicBlock::iterator I = b->begin(), IE = b->end(); I != IE; ++I) {
    if (isa<PHINode>(I)) {
      return true;
    }
  }
  return false;
}

void SplitBasicBlock::shuffle(std::vector<int> &vec) {
  int n = vec.size();
  for (int i = n - 1; i > 0; --i) {
    std::swap(vec[i], vec[cryptoutils->get_uint32_t() % (i + 1)]);
  }
}
