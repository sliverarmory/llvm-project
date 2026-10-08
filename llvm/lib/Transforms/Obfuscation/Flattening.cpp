//===- Flattening.cpp - Flattening Obfuscation pass------------------------===//
//
//                     The LLVM Compiler Infrastructure
//
// This file is distributed under the University of Illinois Open Source
// License. See LICENSE.TXT for details.
//
//===----------------------------------------------------------------------===//
//
// This file implements the flattening pass
//
//===----------------------------------------------------------------------===//

#include "EHRegion.h"
#include "llvm/Transforms/Obfuscation/Flattening.h"
#include "llvm/Transforms/Obfuscation/CryptoUtils.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/IR/CFG.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/Transforms/Utils.h"
#include "llvm/Transforms/Utils/LowerSwitch.h"
#include <algorithm>
#include <iterator>
#include <memory>

#define DEBUG_TYPE "flattening"

using namespace llvm;

// Stats
STATISTIC(Flattened, "Functions flattened");

namespace {
struct Flattening : public FunctionPass {
  static char ID; // Pass identification, replacement for typeid
  bool flag = false;

  Flattening() : FunctionPass(ID) {}
  Flattening(bool flag) : FunctionPass(ID), flag(flag) {}

  bool runOnFunction(Function &F) override;
  bool flatten(Function *f);
};
} // namespace

char Flattening::ID = 0;
static RegisterPass<Flattening> X("flattening", "Call graph flattening");
Pass *llvm::createFlattening(bool flag) { return new Flattening(flag); }

// Full-function flattening cannot move invokes or EH pads through its shared
// dispatcher. Within an EH function, route only ordinary conditional branches
// through their own state dispatchers. A dedicated dispatcher keeps each
// successor's PHI edge and the dominance of values from the source block.
static bool flattenNormalEHBranches(Function &F) {
  SmallPtrSet<BasicBlock *, 32> Protected;
  obfuscation::collectProtectedEHBlocks(F, Protected);

  SmallVector<BranchInst *, 32> Candidates;
  for (BasicBlock &BB : F) {
    if (Protected.contains(&BB) || hasMustTailCall(BB))
      continue;
    auto *Br = dyn_cast<BranchInst>(BB.getTerminator());
    if (!Br || !Br->isConditional() ||
        Br->getSuccessor(0) == Br->getSuccessor(1) ||
        Protected.contains(Br->getSuccessor(0)) ||
        Protected.contains(Br->getSuccessor(1)))
      continue;
    bool Unsafe = false;
    for (Instruction &I : BB) {
      Unsafe |= I.getType()->isTokenTy();
      if (auto *Call = dyn_cast<CallBase>(&I))
        Unsafe |= Call->isConvergent() ||
                  Call->countOperandBundlesOfType(
                      LLVMContext::OB_convergencectrl) != 0 ||
                  Call->countOperandBundlesOfType(LLVMContext::OB_funclet) !=
                      0;
    }
    if (!Unsafe)
      Candidates.push_back(Br);
  }
  if (Candidates.empty()) {
    reportObfuscationSkip("fla", "function", F.getName(),
                          "no-eligible-normal-branch");
    return false;
  }

  // Bound the additional blocks and switch cases in large generated Rust
  // functions. The rest of the ordinary CFG remains valid and untouched.
  if (Candidates.size() > 256) {
    reportObfuscationSkip("fla", "function", F.getName(), "growth-limit");
    Candidates.resize(256);
  }

  LLVMContext &Ctx = F.getContext();
  IntegerType *I32 = Type::getInt32Ty(Ctx);
  IRBuilder<> EntryBuilder(&*F.getEntryBlock().getFirstInsertionPt());
  AllocaInst *StateSlot = EntryBuilder.CreateAlloca(I32, nullptr,
                                                    "obf.eh.state");
  BasicBlock *Default = BasicBlock::Create(Ctx, "eh.dispatch.default", &F);
  new UnreachableInst(Ctx, Default);
  char Key[16];
  cryptoutils->get_bytes(Key, sizeof(Key));

  for (unsigned Index = 0; Index < Candidates.size(); ++Index) {
    BranchInst *Br = Candidates[Index];
    BasicBlock *Source = Br->getParent();
    BasicBlock *TrueDest = Br->getSuccessor(0);
    BasicBlock *FalseDest = Br->getSuccessor(1);
    ConstantInt *TrueState = ConstantInt::get(
        I32, cryptoutils->scramble32(2 * Index, Key));
    ConstantInt *FalseState = ConstantInt::get(
        I32, cryptoutils->scramble32(2 * Index + 1, Key));
    BasicBlock *Dispatcher = BasicBlock::Create(Ctx, "eh.dispatch", &F);

    IRBuilder<> SourceBuilder(Br);
    Value *ChosenState = SourceBuilder.CreateSelect(
        Br->getCondition(), TrueState, FalseState, "obf.eh.choice");
    SourceBuilder.CreateStore(ChosenState, StateSlot)->setVolatile(true);
    SourceBuilder.CreateBr(Dispatcher);
    Br->eraseFromParent();

    IRBuilder<> DispatchBuilder(Dispatcher);
    LoadInst *Loaded =
        DispatchBuilder.CreateLoad(I32, StateSlot, "obf.eh.loaded");
    Loaded->setVolatile(true);
    SwitchInst *Switch = DispatchBuilder.CreateSwitch(Loaded, Default, 2);
    Switch->addCase(TrueState, TrueDest);
    Switch->addCase(FalseState, FalseDest);
    for (BasicBlock *Dest : {TrueDest, FalseDest})
      for (PHINode &Phi : Dest->phis())
        Phi.replaceIncomingBlockWith(Source, Dispatcher);
  }
  ++Flattened;
  reportObfuscationEffect("fla", "function", F.getName(), Candidates.size());
  return true;
}

PreservedAnalyses FlatteningPass::run(Function &F,
                                      FunctionAnalysisManager &AM) {
  // The legacy rewriter handles ordinary branches and exits only. Reject
  // exceptional and indirect control flow before lowering any switches.
  if (!toObfuscate(Flag, &F, "fla"))
    return PreservedAnalyses::all();
  if (F.isConvergent()) {
    reportObfuscationSkip("fla", "function", F.getName(), "convergent");
    return PreservedAnalyses::all();
  }
  if (obfuscation::hasExceptionalControlFlow(F))
    return flattenNormalEHBranches(F) ? PreservedAnalyses::none()
                                      : PreservedAnalyses::all();
  Instruction *EntryTerm = F.getEntryBlock().getTerminator();
  // The legacy dispatcher moves the entry block outside its case table.
  // Re-entering it from a loop would repeat the initial-state store.
  if (!pred_empty(&F.getEntryBlock())) {
    reportObfuscationSkip("fla", "function", F.getName(), "entry-backedge");
    return PreservedAnalyses::all();
  }
  if (!isa<BranchInst>(EntryTerm) && !isa<SwitchInst>(EntryTerm)) {
    reportObfuscationSkip("fla", "function", F.getName(),
                          "entry-terminator");
    return PreservedAnalyses::all();
  }

  bool HasSwitch = false;
  for (BasicBlock &BB : F) {
    if (BB.isEHPad()) {
      reportObfuscationSkip("fla", "function", F.getName(), "eh-pad");
      return PreservedAnalyses::all();
    }
    if (hasMustTailCall(BB)) {
      reportObfuscationSkip("fla", "function", F.getName(), "musttail");
      return PreservedAnalyses::all();
    }
    // The dispatcher changes the cycles and control dependence around
    // convergent operations. Its stack repair also cannot demote token values.
    for (Instruction &I : BB) {
      if (I.getType()->isTokenTy()) {
        reportObfuscationSkip("fla", "function", F.getName(), "token");
        return PreservedAnalyses::all();
      }
      if (auto *Call = dyn_cast<CallBase>(&I)) {
        if (Call->isConvergent() ||
            Call->countOperandBundlesOfType(LLVMContext::OB_convergencectrl)) {
          reportObfuscationSkip("fla", "function", F.getName(),
                                "convergent-call");
          return PreservedAnalyses::all();
        }
      }
    }
    Instruction *Term = BB.getTerminator();
    if (isa<SwitchInst>(Term)) {
      HasSwitch = true;
      continue;
    }
    if (!isa<BranchInst>(Term) && !isa<ReturnInst>(Term) &&
        !isa<UnreachableInst>(Term)) {
      reportObfuscationSkip("fla", "function", F.getName(),
                            "unsupported-terminator");
      return PreservedAnalyses::all();
    }
  }

  // Lower switches here so the flattening code never sees a multiway switch.
  // This is also needed at -O0, where optnone skips optional passes.
  if (HasSwitch)
    LowerSwitchPass().run(F, AM);

  std::unique_ptr<Pass> Legacy(createFlattening(Flag));
  bool Flattened = static_cast<FunctionPass *>(Legacy.get())->runOnFunction(F);
  return HasSwitch || Flattened ? PreservedAnalyses::none()
                                : PreservedAnalyses::all();
}

bool Flattening::runOnFunction(Function &F) {
  Function *tmp = &F;
  // Do we obfuscate
  if (toObfuscate(flag, tmp, "fla")) {
    bool Changed = flatten(tmp);
    if (Changed) {
      ++Flattened;
      reportObfuscationEffect("fla", "function", F.getName());
    }
    return Changed;
  }

  return false;
}

bool Flattening::flatten(Function *f) {
  std::vector<BasicBlock *> origBB;
  BasicBlock *loopEntry;
  BasicBlock *loopEnd;
  LoadInst *load;
  SwitchInst *switchI;
  AllocaInst *switchVar;

  // SCRAMBLER
  char scrambling_key[16];
  llvm::cryptoutils->get_bytes(scrambling_key, 16);
  // END OF SCRAMBLER

#if LLVM_VERSION_MAJOR >= 9
    // >=9.0, LowerSwitchPass depends on LazyValueInfoWrapperPass, which cause AssertError.
    // So I move LowerSwitchPass into register function, just before FlatteningPass.
#else
  // Lower switch
  FunctionPass *lower = createLowerSwitchPass();
  lower->runOnFunction(*f);
#endif

  // Save all original BB
  for (Function::iterator i = f->begin(); i != f->end(); ++i) {
    BasicBlock *tmp = &*i;
    origBB.push_back(tmp);

    BasicBlock *bb = &*i;
    if (isa<InvokeInst>(bb->getTerminator())) {
      reportObfuscationSkip("fla", "function", f->getName(),
                            "unsupported-terminator");
      return false;
    }
  }

  // Nothing to flatten
  if (origBB.size() <= 1) {
    reportObfuscationSkip("fla", "function", f->getName(), "single-block");
    return false;
  }

  // Remove first BB
  origBB.erase(origBB.begin());

  // Get a pointer on the first BB
  Function::iterator tmp = f->begin(); //++tmp;
  BasicBlock *insert = &*tmp;

  // If main begin with an if
  BranchInst *br = NULL;
  if (isa<BranchInst>(insert->getTerminator())) {
    br = cast<BranchInst>(insert->getTerminator());
  }

  unsigned InitialIndex = 0;
  if ((br != NULL && br->isConditional()) ||
      insert->getTerminator()->getNumSuccessors() > 1) {
    BasicBlock::iterator i = insert->end();
    --i;

    if (insert->size() > 1) {
      --i;
    }

    BasicBlock *tmpBB = insert->splitBasicBlock(i, "first");
    origBB.insert(origBB.begin(), tmpBB);
  } else {
    // An unconditional entry can branch to any original block. Resolve its
    // case before mutating the CFG so an unsupported entry leaves no edits.
    auto *InitialBranch = dyn_cast<BranchInst>(insert->getTerminator());
    if (!InitialBranch || !InitialBranch->isUnconditional()) {
      reportObfuscationSkip("fla", "function", f->getName(),
                            "entry-not-unconditional");
      return false;
    }
    auto InitialCase = std::find(origBB.begin(), origBB.end(),
                                 InitialBranch->getSuccessor(0));
    if (InitialCase == origBB.end()) {
      reportObfuscationSkip("fla", "function", f->getName(),
                            "entry-successor-unsupported");
      return false;
    }
    InitialIndex = std::distance(origBB.begin(), InitialCase);
  }

  // The first state must follow the entry's original successor. Optimized
  // Rust often places its return block before the loop body in IR order, so
  // assuming case zero can dispatch directly to the return with an
  // uninitialized value.
  // Remove jump
  insert->getTerminator()->eraseFromParent();

  // Create switch variable and set as it
  switchVar =
      new AllocaInst(Type::getInt32Ty(f->getContext()), 0, "switchVar", insert);
  new StoreInst(
      ConstantInt::get(Type::getInt32Ty(f->getContext()),
                       llvm::cryptoutils->scramble32(InitialIndex,
                                                     scrambling_key)),
      switchVar, insert);

  // Create main loop
  loopEntry = BasicBlock::Create(f->getContext(), "loopEntry", f, insert);
  loopEnd = BasicBlock::Create(f->getContext(), "loopEnd", f, insert);

  load = new LoadInst(switchVar->getAllocatedType(), switchVar, "switchVar",
                      loopEntry);

  // Move first BB on top
  insert->moveBefore(loopEntry);
  BranchInst::Create(loopEntry, insert);

  // loopEnd jump to loopEntry
  BranchInst::Create(loopEntry, loopEnd);

  BasicBlock *swDefault =
      BasicBlock::Create(f->getContext(), "switchDefault", f, loopEnd);
  BranchInst::Create(loopEnd, swDefault);

  // Create switch instruction itself and set condition
  switchI = SwitchInst::Create(&*f->begin(), swDefault, 0, loopEntry);
  switchI->setCondition(load);

  // Remove branch jump from 1st BB and make a jump to the while
  f->begin()->getTerminator()->eraseFromParent();

  BranchInst::Create(loopEntry, &*f->begin());

  // Put all BB in the switch
  for (std::vector<BasicBlock *>::iterator b = origBB.begin();
       b != origBB.end(); ++b) {
    BasicBlock *i = *b;
    ConstantInt *numCase = NULL;

    // Move the BB inside the switch (only visual, no code logic)
    i->moveBefore(loopEnd);

    // Add case to switch
    numCase = cast<ConstantInt>(ConstantInt::get(
        switchI->getCondition()->getType(),
        llvm::cryptoutils->scramble32(switchI->getNumCases(), scrambling_key)));
    switchI->addCase(numCase, i);
  }

  // Recalculate switchVar
  for (std::vector<BasicBlock *>::iterator b = origBB.begin();
       b != origBB.end(); ++b) {
    BasicBlock *i = *b;
    ConstantInt *numCase = NULL;

    // Ret BB
    if (i->getTerminator()->getNumSuccessors() == 0) {
      continue;
    }

    // If it's a non-conditional jump
    if (i->getTerminator()->getNumSuccessors() == 1) {
      // Get successor and delete terminator
      BasicBlock *succ = i->getTerminator()->getSuccessor(0);
      i->getTerminator()->eraseFromParent();

      // Get next case
      numCase = switchI->findCaseDest(succ);

      // If next case == default case (switchDefault)
      if (numCase == NULL) {
        numCase = cast<ConstantInt>(
            ConstantInt::get(switchI->getCondition()->getType(),
                             llvm::cryptoutils->scramble32(
                                 switchI->getNumCases() - 1, scrambling_key)));
      }

      // Update switchVar and jump to the end of loop
      new StoreInst(numCase, load->getPointerOperand(), i);
      BranchInst::Create(loopEnd, i);
      continue;
    }

    // If it's a conditional jump
    if (i->getTerminator()->getNumSuccessors() == 2) {
      // Get next cases
      ConstantInt *numCaseTrue =
          switchI->findCaseDest(i->getTerminator()->getSuccessor(0));
      ConstantInt *numCaseFalse =
          switchI->findCaseDest(i->getTerminator()->getSuccessor(1));

      // Check if next case == default case (switchDefault)
      if (numCaseTrue == NULL) {
        numCaseTrue = cast<ConstantInt>(
            ConstantInt::get(switchI->getCondition()->getType(),
                             llvm::cryptoutils->scramble32(
                                 switchI->getNumCases() - 1, scrambling_key)));
      }

      if (numCaseFalse == NULL) {
        numCaseFalse = cast<ConstantInt>(
            ConstantInt::get(switchI->getCondition()->getType(),
                             llvm::cryptoutils->scramble32(
                                 switchI->getNumCases() - 1, scrambling_key)));
      }

      // Create a SelectInst
      BranchInst *br = cast<BranchInst>(i->getTerminator());
      SelectInst *sel =
          SelectInst::Create(br->getCondition(), numCaseTrue, numCaseFalse, "",
                             i->getTerminator());

      // Erase terminator
      i->getTerminator()->eraseFromParent();

      // Update switchVar and jump to the end of loop
      new StoreInst(sel, load->getPointerOperand(), i);
      BranchInst::Create(loopEnd, i);
      continue;
    }
  }

  fixStack(f);

  return true;
}
