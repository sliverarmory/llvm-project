#include "llvm/Transforms/Obfuscation/Utils.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/IntrinsicInst.h"
#include "llvm/IR/Module.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;

static cl::list<std::string> OnlyFunctions(
    "obf-only-functions", cl::CommaSeparated,
    cl::desc("Apply enabled obfuscation passes only to these exact LLVM "
             "function names"));

static cl::opt<bool> ReportObfuscationSkips(
    "obf-report-skips", cl::init(false),
    cl::desc("Report why requested obfuscation transformations were skipped"));

void reportObfuscationSkip(StringRef Pass, StringRef Kind, StringRef Symbol,
                           StringRef Reason) {
  if (!ReportObfuscationSkips)
    return;
  errs() << "obf-skip pass=" << Pass << " kind=" << Kind << " symbol=\"";
  errs().write_escaped(Symbol);
  errs() << "\" reason=" << Reason << '\n';
}

static bool isAllowedFunction(StringRef Name) {
  if (OnlyFunctions.empty())
    return true;
  for (const std::string &Allowed : OnlyFunctions)
    if (Name == Allowed)
      return true;
  return false;
}

bool hasMustTailCall(const BasicBlock &BB) {
  for (const Instruction &I : BB) {
    if (const auto *Call = dyn_cast<CallInst>(&I)) {
      if (Call->isMustTailCall())
        return true;
    }
  }
  return false;
}

// Shamefully borrowed from ../Scalar/RegToMem.cpp :(
bool valueEscapes(Instruction *Inst) {
  BasicBlock *BB = Inst->getParent();
  for (Value::use_iterator UI = Inst->use_begin(), E = Inst->use_end(); UI != E;
       ++UI) {
    Instruction *I = cast<Instruction>(*UI);
    if (I->getParent() != BB || isa<PHINode>(I)) {
      return true;
    }
  }
  return false;
}

void fixStack(Function *f) {
  // Try to remove phi node and demote reg to stack
  std::vector<PHINode *> tmpPhi;
  std::vector<Instruction *> tmpReg;
  BasicBlock *bbEntry = &*f->begin();

  do {
    tmpPhi.clear();
    tmpReg.clear();

    for (Function::iterator i = f->begin(); i != f->end(); ++i) {

      for (BasicBlock::iterator j = i->begin(); j != i->end(); ++j) {

        if (isa<PHINode>(j)) {
          PHINode *phi = cast<PHINode>(j);
          tmpPhi.push_back(phi);
          continue;
        }
        if (!(isa<AllocaInst>(j) && j->getParent() == bbEntry) &&
            (valueEscapes(&*j) || j->isUsedOutsideOfBlock(&*i))) {
          tmpReg.push_back(&*j);
          continue;
        }
      }
    }
    for (Instruction *Inst : tmpReg) {
      if (auto *AI = dyn_cast<AllocaInst>(Inst)) {
        // Demoting a non-entry alloca replaces its uses with loads of the
        // pointer. LLVM lifetime markers require the alloca itself as their
        // operand, so discard these optional markers before that rewrite.
        SmallVector<IntrinsicInst *, 4> LifetimeMarkers;
        for (User *U : AI->users()) {
          if (auto *II = dyn_cast<IntrinsicInst>(U)) {
            if (II->isLifetimeStartOrEnd())
              LifetimeMarkers.push_back(II);
          }
        }
        for (IntrinsicInst *II : LifetimeMarkers)
          II->eraseFromParent();
      }
      DemoteRegToStack(*Inst, f->begin()->getTerminator());
    }

    for (unsigned int i = 0; i != tmpPhi.size(); ++i) {
      DemotePHIToStack(tmpPhi.at(i));
    }

  } while (tmpReg.size() != 0 || tmpPhi.size() != 0);
}

template <typename CallbackT>
static void forEachFunctionAnnotation(Function *F, CallbackT Callback) {
  GlobalVariable *Annotations =
      F->getParent()->getGlobalVariable("llvm.global.annotations");
  if (!Annotations || !Annotations->hasInitializer())
    return;

  auto *Entries = dyn_cast<ConstantArray>(Annotations->getInitializer());
  if (!Entries)
    return;

  for (const Use &Entry : Entries->operands()) {
    auto *Record = dyn_cast<ConstantStruct>(Entry.get());
    if (!Record || Record->getNumOperands() < 2 ||
        Record->getOperand(0)->stripPointerCasts() != F)
      continue;

    // Modern Clang uses direct pointers; stripPointerCasts also handles the
    // casts and all-zero GEPs used by older annotation records.
    auto *TextGlobal = dyn_cast<GlobalVariable>(
        Record->getOperand(1)->stripPointerCasts());
    if (!TextGlobal || !TextGlobal->hasInitializer())
      continue;
    auto *Data = dyn_cast<ConstantDataSequential>(TextGlobal->getInitializer());
    if (Data && Data->isCString())
      Callback(Data->getAsCString());
  }
}

std::string readAnnotate(Function *F) {
  std::string Annotation;
  forEachFunctionAnnotation(F, [&](StringRef Text) {
    Annotation += Text.lower();
    Annotation += ' ';
  });
  return Annotation;
}

bool toObfuscate(bool flag, Function *f, std::string const &attribute) {
  std::string NegativeAttribute = "no" + attribute;
  bool Positive = false;
  bool Negative = false;
  forEachFunctionAnnotation(f, [&](StringRef Text) {
    Positive |= Text.equals_insensitive(attribute);
    Negative |= Text.equals_insensitive(NegativeAttribute);
  });

  if (Negative) {
    reportObfuscationSkip(attribute, "function", f->getName(),
                          "negative-annotation");
    return false;
  }
  if (!Positive && !flag)
    return false;
  if (f->isDeclaration()) {
    reportObfuscationSkip(attribute, "function", f->getName(), "declaration");
    return false;
  }
  if (f->hasAvailableExternallyLinkage()) {
    reportObfuscationSkip(attribute, "function", f->getName(),
                          "available-externally");
    return false;
  }
  if (!isAllowedFunction(f->getName())) {
    reportObfuscationSkip(attribute, "function", f->getName(), "not-selected");
    return false;
  }
  return true;
}
