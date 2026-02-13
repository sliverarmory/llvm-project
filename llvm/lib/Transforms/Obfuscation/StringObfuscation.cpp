#define DEBUG_TYPE "objdiv"

#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/Statistic.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/GlobalVariable.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/Module.h"
#include "llvm/Pass.h"
#include "llvm/Transforms/Obfuscation/CryptoUtils.h"
#include "llvm/Transforms/Obfuscation/StringObfuscation.h"
#include "llvm/Transforms/Utils/ModuleUtils.h"
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

using namespace llvm;

STATISTIC(GlobalsEncoded, "Counts number of global variables encoded");

namespace llvm {

struct EncodedGlobal {
  GlobalVariable *Var;
  uint8_t Key;
  uint8_t Step;
  uint32_t Size;
};

static bool shouldEncodeGlobal(const GlobalVariable &GV) {
  if (!GV.isConstant() || !GV.hasInitializer())
    return false;
  if (GV.isThreadLocal())
    return false;

  StringRef Section = GV.getSection();
  if (Section == "llvm.metadata" || Section.contains("__objc_methname"))
    return false;

  const auto *CDS = dyn_cast<ConstantDataSequential>(GV.getInitializer());
  if (!CDS || !CDS->isCString())
    return false;
  if (!CDS->getElementType()->isIntegerTy(8))
    return false;
  if (CDS->getRawDataValues().empty())
    return false;

  return true;
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

  LegacyStringObfuscationPass() : ModulePass(ID) {}
  explicit LegacyStringObfuscationPass(bool Flag) : ModulePass(ID), IsFlag(Flag) {}

  bool runOnModule(Module &M) override {
    if (!IsFlag)
      return false;

    SmallVector<GlobalVariable *, 16> ToDelete;
    std::vector<EncodedGlobal> EncodedGlobals;
    bool Changed = false;

    for (Module::global_iterator GI = M.global_begin(), GE = M.global_end();
         GI != GE; ++GI) {
      GlobalVariable *GV = &*GI;
      if (!shouldEncodeGlobal(*GV))
        continue;

      auto *CDS = cast<ConstantDataSequential>(GV->getInitializer());
      uint8_t Key = cryptoutils->get_uint8_t();
      uint8_t Step = static_cast<uint8_t>(cryptoutils->get_uint8_t() | 1U);

      uint32_t Size = 0;
      Constant *EncodedInit =
          buildEncodedInitializer(M, *CDS, Key, Step, Size);
      if (!EncodedInit || Size == 0)
        continue;

      auto *DynGV = new GlobalVariable(
          M, GV->getValueType(),
          /*isConstant=*/false, GV->getLinkage(), EncodedInit, GV->getName(),
          /*InsertBefore=*/nullptr, GV->getThreadLocalMode(),
          GV->getType()->getAddressSpace());
      DynGV->copyAttributesFrom(GV);
      DynGV->setConstant(false);
      DynGV->setInitializer(EncodedInit);

      GV->replaceAllUsesWith(DynGV);
      ToDelete.push_back(GV);
      EncodedGlobals.push_back({DynGV, Key, Step, Size});
      ++GlobalsEncoded;
      Changed = true;
    }

    for (GlobalVariable *GV : ToDelete)
      GV->eraseFromParent();

    if (!EncodedGlobals.empty())
      addDecodeFunction(M, EncodedGlobals);

    return Changed;
  }

private:
  void addDecodeFunction(Module &M, const std::vector<EncodedGlobal> &GVars) {
    FunctionType *FuncTy =
        FunctionType::get(Type::getVoidTy(M.getContext()), {}, false);
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

      Value *Index8 = Builder.CreateTrunc(Index, Builder.getInt8Ty());
      Value *Mask = Builder.CreateAdd(
          Builder.CreateMul(Index8, Builder.getInt8(GVar.Step)),
          Builder.getInt8(GVar.Key), "strdec.mask");
      Value *Decoded = Builder.CreateXor(LoadElement, Mask, "strdec.xor");

      StoreInst *Store = Builder.CreateStore(Decoded, GEP);
      Store->setAlignment(Align(1));

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
  std::unique_ptr<Pass> Legacy(createStringObfuscation(Flag));
  bool Changed = static_cast<ModulePass *>(Legacy.get())->runOnModule(M);
  return Changed ? PreservedAnalyses::none() : PreservedAnalyses::all();
}

Pass *llvm::createStringObfuscation(bool flag) {
  return new LegacyStringObfuscationPass(flag);
}

