#define DEBUG_TYPE "objdiv"

#include "llvm/Analysis/ValueTracking.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/Statistic.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/GlobalVariable.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/IntrinsicInst.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/Metadata.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/Operator.h"
#include "llvm/Pass.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Transforms/Obfuscation/CryptoUtils.h"
#include "llvm/Transforms/Obfuscation/StringObfuscation.h"
#include "llvm/Transforms/Obfuscation/Utils.h"
#include "llvm/Transforms/Utils/ModuleUtils.h"
#include <cstdint>
#include <limits>
#include <memory>
#include <string>
#include <vector>

using namespace llvm;

STATISTIC(GlobalsEncoded, "Counts number of global variables encoded");

static cl::list<std::string> OnlyStringGlobals(
    "sobf-only-globals", cl::CommaSeparated,
    cl::desc("Encode only these exact LLVM global names when string "
             "obfuscation is enabled"));

// The legacy Clang path encodes only C strings. Rust's literal backing arrays
// have no required terminator and may contain embedded NUL or arbitrary bytes.
// This option also makes the Rust rules available to direct opt tests.
static cl::opt<bool> EncodeRustByteArrays(
    "sobf-rust-bytes", cl::init(false),
    cl::desc("Encode eligible Rust-style byte-array literals"));

namespace llvm {

struct EncodedGlobal {
  GlobalVariable *Var;
  uint8_t Key;
  uint8_t Step;
  uint32_t Size;
};

static bool isObjCRuntimeName(StringRef Name) {
  return Name.starts_with("OBJC_") || Name.starts_with("_OBJC_") ||
         Name.starts_with("._OBJC_") || Name.starts_with(".objc_") ||
         Name.starts_with("_objc_") || Name.starts_with("__objc_") ||
         Name.starts_with(".objcv2_");
}

static bool isObjCRuntimeSection(StringRef Section) {
  return Section.contains("__objc_") || Section.contains("__cfstring") ||
         Section.starts_with("__OBJC,") || Section.starts_with(".objcrt$");
}

// Objective-C runtime strings must be available before global constructors run.
// GNU runtimes also emit unnamed strings whose only distinguishing feature is
// that a generated metadata global (or its registration function) uses them.
static bool isObjCRuntimeMetadata(const GlobalVariable &GV) {
  if (isObjCRuntimeName(GV.getName()) ||
      isObjCRuntimeSection(GV.getSection()))
    return true;

  SmallVector<const User *, 16> Pending(GV.user_begin(), GV.user_end());
  SmallPtrSet<const User *, 32> Seen;
  while (!Pending.empty()) {
    const User *U = Pending.pop_back_val();
    if (!Seen.insert(U).second)
      continue;
    if (const auto *OtherGV = dyn_cast<GlobalVariable>(U)) {
      if (isObjCRuntimeName(OtherGV->getName()) ||
          isObjCRuntimeSection(OtherGV->getSection()))
        return true;
    }
    if (const auto *I = dyn_cast<Instruction>(U)) {
      const Function *F = I->getFunction();
      if (F && isObjCRuntimeName(F->getName()))
        return true;
      continue;
    }
    if (const auto *C = dyn_cast<Constant>(U))
      for (const User *Parent : C->users())
        Pending.push_back(Parent);
  }
  return false;
}

static bool isNamedForEncoding(StringRef Name) {
  if (OnlyStringGlobals.empty())
    return true;
  for (const std::string &Allowed : OnlyStringGlobals)
    if (Name == Allowed)
      return true;
  return false;
}

static bool hasByteArrayInitializer(const GlobalVariable &GV) {
  if (!GV.hasInitializer())
    return false;
  const auto *CDS = dyn_cast<ConstantDataSequential>(GV.getInitializer());
  return CDS && CDS->isString(8) &&
         !CDS->getRawDataValues().empty();
}

static bool hasCStringInitializer(const GlobalVariable &GV) {
  // Check the size first: isCString() reads the last byte.
  return hasByteArrayInitializer(GV) &&
         cast<ConstantDataSequential>(GV.getInitializer())->isCString();
}

// A decoder registered at priority zero precedes constructors with larger
// priorities. Equal priorities have unspecified order. Include direct callees
// so a selected byte array cannot be read through a helper before decoding.
static void collectEarlyCtorReaders(
    const Module &M, SmallPtrSetImpl<const Function *> &EarlyReaders,
    bool &HasIndirectCall) {
  const GlobalVariable *Ctors = M.getNamedGlobal("llvm.global_ctors");
  if (!Ctors || !Ctors->hasInitializer())
    return;
  const auto *Entries = dyn_cast<ConstantArray>(Ctors->getInitializer());
  if (!Entries) {
    HasIndirectCall = true;
    return;
  }

  SmallVector<const Function *, 8> Pending;
  for (const Use &Entry : Entries->operands()) {
    const auto *Record = dyn_cast<ConstantStruct>(Entry.get());
    if (!Record || Record->getNumOperands() < 2) {
      HasIndirectCall = true;
      continue;
    }
    const auto *Priority = dyn_cast<ConstantInt>(Record->getOperand(0));
    if (!Priority) {
      HasIndirectCall = true;
      continue;
    }
    if (!Priority->isZero())
      continue;
    const auto *F = dyn_cast<Function>(
        Record->getOperand(1)->stripPointerCasts());
    if (!F) {
      HasIndirectCall = true;
      continue;
    }
    Pending.push_back(F);
  }

  while (!Pending.empty()) {
    const Function *F = Pending.pop_back_val();
    if (!EarlyReaders.insert(F).second)
      continue;
    for (const BasicBlock &BB : *F) {
      for (const Instruction &I : BB) {
        const auto *CB = dyn_cast<CallBase>(&I);
        if (!CB)
          continue;
        const Function *Callee = CB->getCalledFunction();
        if (!Callee) {
          HasIndirectCall = true;
          continue;
        }
        if (!Callee->isDeclaration())
          Pending.push_back(Callee);
      }
    }
  }
}

// Follow a Rust allocation through constant pointer/length descriptors and
// transparent pointer operations. Any unsupported or unselected user makes a
// whole-global decision: replacing only some reads would be misleading, while
// a read from a priority-zero constructor would observe the encoded bytes.
static StringRef rustByteUseSkipReason(
    GlobalVariable &GV, const SmallPtrSetImpl<const Function *> &EarlyReaders,
    bool HasIndirectEarlyCall) {
  if (HasIndirectEarlyCall)
    return "early-ctor-indirect-call";

  SmallVector<Value *, 16> Pending{&GV};
  SmallPtrSet<Value *, 32> Seen;
  SmallPtrSet<const Function *, 16> CheckedFunctions;
  bool HasRuntimeUse = false;
  while (!Pending.empty()) {
    Value *V = Pending.pop_back_val();
    if (!Seen.insert(V).second)
      continue;
    for (User *U : V->users()) {
      if (auto *OtherGV = dyn_cast<GlobalVariable>(U)) {
        if (!OtherGV->hasLocalLinkage() || !OtherGV->isConstant() ||
            OtherGV->isThreadLocal() || OtherGV->hasSection() ||
            OtherGV->hasComdat() || OtherGV->isExternallyInitialized() ||
            OtherGV->hasMetadataOtherThanDebugLocAndGuid() ||
            OtherGV->getName().starts_with("llvm.") ||
            isObjCRuntimeMetadata(*OtherGV))
          return "unsafe-global-user";
        Pending.push_back(OtherGV);
        continue;
      }
      if (isa<GlobalValue>(U))
        return "alias-or-ifunc";
      if (auto *C = dyn_cast<Constant>(U)) {
        if (auto *CE = dyn_cast<ConstantExpr>(C)) {
          if (CE->getOpcode() != Instruction::BitCast &&
              !(CE->getOpcode() == Instruction::GetElementPtr &&
                cast<GEPOperator>(CE)->isInBounds()))
            return "unsupported-constant-use";
        } else if (!isa<ConstantStruct>(C) && !isa<ConstantArray>(C)) {
          return "unsupported-constant-use";
        }
        Pending.push_back(C);
        continue;
      }

      auto *I = dyn_cast<Instruction>(U);
      if (!I || !I->getFunction())
        return "unsupported-use";
      Function *F = I->getFunction();
      if (EarlyReaders.contains(F))
        return "early-initialization";
      if (CheckedFunctions.insert(F).second &&
          !toObfuscate(/*flag=*/true, F, "sobf"))
        return "function-not-selected";

      if (isa<AddrSpaceCastInst>(I))
        return "pointer-escapes";
      if (isa<PtrToIntInst>(I) || isa<ICmpInst>(I) ||
          isa<ReturnInst>(I)) {
        HasRuntimeUse = true;
        continue;
      }
      if (const auto *SI = dyn_cast<StoreInst>(I)) {
        if (SI->getPointerOperand() == V)
          return "written-before-decode";
        if (!isa<AllocaInst>(getUnderlyingObject(SI->getPointerOperand())))
          return "pointer-escapes";
        HasRuntimeUse = true;
        continue;
      }
      if (const auto *LI = dyn_cast<LoadInst>(I)) {
        if (LI->isAtomic() || LI->isVolatile())
          return "atomic-or-volatile";
        HasRuntimeUse = true;
        continue;
      }
      if (const auto *CB = dyn_cast<CallBase>(I)) {
        if (CB->isInlineAsm() || CB->getCalledOperand() == V)
          return "pointer-escapes";
        if (const Function *Callee = CB->getCalledFunction()) {
          if (Callee->getName().starts_with("llvm.invariant.") ||
              Callee->getName().starts_with("llvm.type.") ||
              ((Callee->getName().starts_with("llvm.memcpy.") ||
                Callee->getName().starts_with("llvm.memmove.") ||
                Callee->getName().starts_with("llvm.memset.")) &&
               CB->arg_size() > 0 && CB->getArgOperand(0) == V))
            return "unsupported-intrinsic-use";
        }
        HasRuntimeUse = true;
        continue;
      }
      if (isa<GetElementPtrInst>(I) || isa<BitCastInst>(I) ||
          isa<PHINode>(I) || isa<SelectInst>(I) ||
          isa<InsertValueInst>(I) || isa<ExtractValueInst>(I)) {
        Pending.push_back(I);
        continue;
      }
      return "unsupported-use";
    }
  }
  return HasRuntimeUse ? StringRef() : StringRef("no-runtime-use");
}

// An empty reason means the global is safe to encode.
static StringRef stringSkipReason(
    GlobalVariable &GV, bool RustByteArrays,
    const SmallPtrSetImpl<const Function *> &EarlyReaders,
    bool HasIndirectEarlyCall) {
  if (GV.hasMetadata("obf.sobf"))
    return "already-encoded";
  if (GV.hasMetadata("obf.constenc"))
    return "generated-constant";
  if (!GV.hasInitializer())
    return "no-initializer";
  if (!GV.isConstant())
    return "mutable";
  if (GV.isThreadLocal())
    return "thread-local";
  // Multiple translation units can emit the same weak/ODR global with
  // different keys. The linker keeps one definition but runs every decoder.
  if (GV.isWeakForLinker() || GV.hasAvailableExternallyLinkage() ||
      GV.hasComdat())
    return "weak-or-comdat";

  StringRef Section = GV.getSection();
  if (Section == "llvm.metadata")
    return "llvm-metadata";
  if (isObjCRuntimeMetadata(GV))
    return "objc-metadata";

  if (RustByteArrays) {
    if (!hasByteArrayInitializer(GV))
      return "not-byte-array";
  } else if (!hasCStringInitializer(GV)) {
    return "not-c-string";
  }
  if (!isNamedForEncoding(GV.getName()))
    return "not-selected";

  const auto *CDS = cast<ConstantDataSequential>(GV.getInitializer());
  if (CDS->getRawDataValues().size() >
      std::numeric_limits<uint32_t>::max())
    return "oversized-array";

  if (RustByteArrays) {
    if (!GV.hasLocalLinkage())
      return "non-local";
    if (GV.getAddressSpace() != 0)
      return "address-space";
    if (GV.hasSection())
      return "explicit-section";
    if (GV.isExternallyInitialized())
      return "external-init";
    if (GV.hasMetadataOtherThanDebugLocAndGuid())
      return "metadata";
    if (StringRef Reason = rustByteUseSkipReason(
            GV, EarlyReaders, HasIndirectEarlyCall);
        !Reason.empty())
      return Reason;
  }

  return {};
}

static Constant *buildEncodedInitializer(Module &M,
                                         const ConstantDataSequential &CDS,
                                         uint8_t Key, uint8_t Step,
                                         uint32_t &Size) {
  StringRef Raw = CDS.getRawDataValues();
  Size = static_cast<uint32_t>(Raw.size());
  if (Raw.empty())
    return nullptr;

  SmallVector<uint8_t, 64> Encoded;
  Encoded.reserve(Raw.size());
  for (size_t I = 0; I < Raw.size(); ++I) {
    uint8_t Mask = static_cast<uint8_t>(Key + static_cast<uint8_t>(I * Step));
    Encoded.push_back(static_cast<uint8_t>(Raw[I]) ^ Mask);
  }

  return ConstantDataArray::get(M.getContext(), Encoded);
}

class LegacyStringObfuscationPass : public ModulePass {
public:
  static char ID;
  bool IsFlag = true;
  bool RustByteArrays = false;

  LegacyStringObfuscationPass() : ModulePass(ID) {}
  explicit LegacyStringObfuscationPass(bool Flag, bool RustByteArrays = false)
      : ModulePass(ID), IsFlag(Flag), RustByteArrays(RustByteArrays) {}

  bool runOnModule(Module &M) override {
    if (!IsFlag)
      return false;

    SmallVector<GlobalVariable *, 16> ToDelete;
    std::vector<EncodedGlobal> EncodedGlobals;
    bool Changed = false;
    SmallPtrSet<const Function *, 16> EarlyReaders;
    bool HasIndirectEarlyCall = false;
    if (RustByteArrays)
      collectEarlyCtorReaders(M, EarlyReaders, HasIndirectEarlyCall);

    for (Module::global_iterator GI = M.global_begin(), GE = M.global_end();
         GI != GE; ++GI) {
      GlobalVariable *GV = &*GI;
      // Ordinary non-string globals are outside this pass. An explicitly
      // named global is still reported if it cannot be encoded safely.
      if (!(RustByteArrays ? hasByteArrayInitializer(*GV)
                           : hasCStringInitializer(*GV)) &&
          (OnlyStringGlobals.empty() || !isNamedForEncoding(GV->getName())))
        continue;
      if (StringRef Reason = stringSkipReason(
              *GV, RustByteArrays, EarlyReaders, HasIndirectEarlyCall);
          !Reason.empty()) {
        reportObfuscationSkip("sobf", "global", GV->getName(), Reason);
        continue;
      }

      auto *CDS = cast<ConstantDataSequential>(GV->getInitializer());
      setObfuscationRandomContext("sobf", GV->getName());
      uint8_t Key = cryptoutils->get_uint8_t();
      uint8_t Step = static_cast<uint8_t>(cryptoutils->get_uint8_t() | 1U);

      uint32_t Size = 0;
      Constant *EncodedInit =
          buildEncodedInitializer(M, *CDS, Key, Step, Size);
      if (!EncodedInit || Size == 0) {
        reportObfuscationSkip("sobf", "global", GV->getName(), "empty-string");
        continue;
      }

      auto *DynGV = new GlobalVariable(
          M, GV->getValueType(),
          /*isConstant=*/false, GV->getLinkage(), EncodedInit, "",
          /*InsertBefore=*/nullptr, GV->getThreadLocalMode(),
          GV->getType()->getAddressSpace());
      DynGV->copyAttributesFrom(GV);
      DynGV->setConstant(false);
      DynGV->setInitializer(EncodedInit);
      if (RustByteArrays)
        DynGV->setMetadata("obf.sobf", MDNode::get(M.getContext(), {}));

      GV->replaceAllUsesWith(DynGV);
      // Keep externally visible symbols stable when replacing their storage.
      DynGV->takeName(GV);
      ToDelete.push_back(GV);
      EncodedGlobals.push_back({DynGV, Key, Step, Size});
      reportObfuscationEffect("sobf", "global", DynGV->getName());
      ++GlobalsEncoded;
      Changed = true;
    }

    for (GlobalVariable *GV : ToDelete)
      GV->eraseFromParent();

    if (!EncodedGlobals.empty())
      addDecodeFunction(M, EncodedGlobals);

    for (const std::string &Selected : OnlyStringGlobals)
      if (!M.getNamedGlobal(Selected))
        reportObfuscationSkip("sobf", "global", Selected, "not-found");

    return Changed;
  }

private:
  void addDecodeFunction(Module &M, const std::vector<EncodedGlobal> &GVars) {
    FunctionType *FuncTy =
        FunctionType::get(Type::getVoidTy(M.getContext()), {}, false);
    std::string DecoderIdentity = "decoder";
    for (const EncodedGlobal &GVar : GVars) {
      StringRef Name = GVar.Var->getName();
      DecoderIdentity += "/" + std::to_string(Name.size()) + ":" + Name.str();
    }
    setObfuscationRandomContext("sobf", DecoderIdentity);
    std::string Name =
        ".datadiv_decode" + std::to_string(cryptoutils->get_uint64_t());
    FunctionCallee Callee = M.getOrInsertFunction(Name, FuncTy);
    Function *DecodeFn = cast<Function>(Callee.getCallee());
    DecodeFn->setCallingConv(CallingConv::C);
    DecodeFn->setLinkage(GlobalValue::PrivateLinkage);

    BasicBlock *Entry = BasicBlock::Create(M.getContext(), "entry", DecodeFn);
    IRBuilder<> Builder(Entry);

    for (const EncodedGlobal &GVar : GVars) {
      if (GVar.Size == 0)
        continue;

      BasicBlock *PreHeaderBB = Builder.GetInsertBlock();
      BasicBlock *ForBody =
          BasicBlock::Create(M.getContext(), "strdec.body", DecodeFn);
      BasicBlock *ForEnd =
          BasicBlock::Create(M.getContext(), "strdec.end", DecodeFn);
      Builder.CreateBr(ForBody);
      Builder.SetInsertPoint(ForBody);

      PHINode *Index = Builder.CreatePHI(Builder.getInt32Ty(), 2, "i");
      Index->addIncoming(Builder.getInt32(0), PreHeaderBB);

      Value *IndexList[2] = {Builder.getInt32(0), Index};
      Value *GEP = Builder.CreateGEP(
          GVar.Var->getValueType(), GVar.Var,
          ArrayRef<Value *>(IndexList, 2), "strdec.gep");
      LoadInst *LoadElement = Builder.CreateLoad(Builder.getInt8Ty(), GEP);
      LoadElement->setAlignment(Align(1));
      // Rust runs this decoder at pipeline start. Without observable memory
      // accesses, GlobalOpt can evaluate a small decoder and restore the
      // plaintext initializer in the final artifact.
      LoadElement->setVolatile(RustByteArrays);

      Value *Index8 = Builder.CreateTrunc(Index, Builder.getInt8Ty());
      Value *Mask = Builder.CreateAdd(
          Builder.CreateMul(Index8, Builder.getInt8(GVar.Step)),
          Builder.getInt8(GVar.Key), "strdec.mask");
      Value *Decoded = Builder.CreateXor(LoadElement, Mask, "strdec.xor");

      StoreInst *Store = Builder.CreateStore(Decoded, GEP);
      Store->setAlignment(Align(1));
      Store->setVolatile(RustByteArrays);

      Value *NextValue =
          Builder.CreateAdd(Index, Builder.getInt32(1), "next-value");
      Value *EndCondition = Builder.CreateICmpULT(
          NextValue, Builder.getInt32(GVar.Size), "loop-condition");
      BasicBlock *LoopEndBB = Builder.GetInsertBlock();
      Builder.CreateCondBr(EndCondition, ForBody, ForEnd);
      Index->addIncoming(NextValue, LoopEndBB);
      Builder.SetInsertPoint(ForEnd);
    }

    Builder.CreateRetVoid();
    appendToGlobalCtors(M, DecodeFn, 0);
  }
};

} // namespace llvm

char LegacyStringObfuscationPass::ID = 0;
static RegisterPass<LegacyStringObfuscationPass>
    X("GVDiv", "Global variable (i.e., const char*) diversification pass",
      false, true);

PreservedAnalyses StringObfuscationPass::run(Module &M,
                                             ModuleAnalysisManager &AM) {
  (void)AM;
  std::unique_ptr<Pass> Legacy(new LegacyStringObfuscationPass(
      Flag, RustByteArrays || EncodeRustByteArrays));
  bool Changed = static_cast<ModulePass *>(Legacy.get())->runOnModule(M);
  return Changed ? PreservedAnalyses::none() : PreservedAnalyses::all();
}

Pass *llvm::createStringObfuscation(bool flag) {
  return new LegacyStringObfuscationPass(flag);
}
