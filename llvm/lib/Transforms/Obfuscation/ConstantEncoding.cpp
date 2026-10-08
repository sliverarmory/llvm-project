//===- ConstantEncoding.cpp - Selected integer constant encoding --------===//

#include "llvm/Transforms/Obfuscation/ConstantEncoding.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/GlobalVariable.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/Module.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Transforms/Obfuscation/CryptoUtils.h"
#include "llvm/Transforms/Obfuscation/OptionParser.h"
#include "llvm/Transforms/Obfuscation/Utils.h"
#include <cstdint>
#include <string>

using namespace llvm;

namespace {

struct TypedValue {
  unsigned Width;
  uint64_t Bits;
};

static bool parseTypedValue(StringRef Text, TypedValue &Parsed) {
  auto Parts = Text.split(':');
  StringRef Type = Parts.first;
  StringRef Value = Parts.second;
  if (!Type.consume_front("i") || Type.empty() || Value.empty() ||
      Type.getAsInteger(10, Parsed.Width) ||
      (Parsed.Width != 8 && Parsed.Width != 16 && Parsed.Width != 32 &&
       Parsed.Width != 64))
    return false;

  uint64_t Mask = Parsed.Width == 64 ? UINT64_MAX :
                                          (uint64_t(1) << Parsed.Width) - 1;
  if (Value.starts_with("-")) {
    int64_t Signed = 0;
    if (Value.getAsInteger(0, Signed))
      return false;
    if (Parsed.Width != 64 &&
        Signed < -(int64_t(1) << (Parsed.Width - 1)))
      return false;
    Parsed.Bits = static_cast<uint64_t>(Signed) & Mask;
    return true;
  }

  if (Value.starts_with("+"))
    return false;
  if (Value.getAsInteger(0, Parsed.Bits) || Parsed.Bits > Mask)
    return false;
  return true;
}

class TypedValueParser : public cl::parser<std::string> {
public:
  explicit TypedValueParser(cl::Option &O) : cl::parser<std::string>(O) {}

  bool parse(cl::Option &O, StringRef ArgName, StringRef Arg,
             std::string &Value) {
    if (cl::parser<std::string>::parse(O, ArgName, Arg, Value))
      return true;
    TypedValue Parsed{};
    if (!parseTypedValue(Value, Parsed))
      return O.error("expected i8/i16/i32/i64:<unsigned bits or signed value>");
    return false;
  }
};

static cl::list<std::string, bool, TypedValueParser> SelectedValues(
    "constenc-values", cl::CommaSeparated,
    cl::desc("Exact typed integer values to encode, such as i32:0x1234"));

static cl::opt<int, false, obfuscation::RangedIntParser<0>> MaxSites(
    "constenc-max-sites", cl::init(64),
    cl::desc("Maximum encoded integer constant operands per function"));

static constexpr StringLiteral GeneratedTag = "obf.constenc";

struct Site {
  Instruction *Inst;
  unsigned Operand;
  Value *Other;
  ConstantInt *Constant;
};

static bool selected(const ConstantInt &C, ArrayRef<TypedValue> Values) {
  unsigned Width = C.getBitWidth();
  if (Width != 8 && Width != 16 && Width != 32 && Width != 64)
    return false;
  uint64_t Bits = C.getZExtValue();
  for (const TypedValue &Value : Values)
    if (Value.Width == Width && Value.Bits == Bits)
      return true;
  return false;
}

static bool isEligibleInstruction(const Instruction &I) {
  if (I.getMetadata(GeneratedTag) ||
      I.getMetadata("obfuscation.bcf.generated"))
    return false;
  if (const auto *BO = dyn_cast<BinaryOperator>(&I))
    return BO->getType()->isIntegerTy();
  return isa<ICmpInst>(I);
}

static void tag(Value *V, MDNode *Marker) {
  if (auto *I = dyn_cast<Instruction>(V))
    I->setMetadata(GeneratedTag, Marker);
}

static GlobalVariable *makeShares(Module &M, IntegerType *Ty,
                                  uint64_t First, uint64_t Second,
                                  StringRef Name, MDNode *Marker) {
  Constant *Rows[2] = {ConstantInt::get(Ty, First),
                       ConstantInt::get(Ty, Second)};
  ArrayType *RowsTy = ArrayType::get(Ty, 2);
  auto *GV = new GlobalVariable(M, RowsTy, /*isConstant=*/true,
                                GlobalValue::PrivateLinkage,
                                ConstantArray::get(RowsTy, Rows), Name);
  GV->setMetadata(GeneratedTag, Marker);
  return GV;
}

static Value *loadShare(IRBuilder<> &Builder, GlobalVariable *Rows,
                        Value *Index, IntegerType *Ty, MDNode *Marker) {
  Value *Indices[2] = {Builder.getInt32(0), Index};
  Value *Ptr = Builder.CreateInBoundsGEP(Rows->getValueType(), Rows, Indices,
                                         "obf.const.ptr");
  tag(Ptr, Marker);
  Value *Loaded = Builder.CreateLoad(Ty, Ptr, "obf.const.load");
  tag(Loaded, Marker);
  return Loaded;
}

static void encodeSite(Module &M, const Site &S, MDNode *Marker) {
  auto *Ty = cast<IntegerType>(S.Constant->getType());
  unsigned Width = Ty->getBitWidth();
  uint64_t Mask = Width == 64 ? UINT64_MAX : (uint64_t(1) << Width) - 1;
  uint64_t A0 = cryptoutils->get_uint64_t() & Mask;
  uint64_t Delta = cryptoutils->get_uint64_t() & Mask;
  if (!Delta)
    Delta = 1;
  uint64_t A1 = A0 ^ Delta;
  uint64_t Bits = S.Constant->getZExtValue();
  GlobalVariable *A = makeShares(M, Ty, A0, A1, ".obf.const.a", Marker);
  GlobalVariable *B =
      makeShares(M, Ty, A0 ^ Bits, A1 ^ Bits, ".obf.const.b", Marker);

  IRBuilder<> Builder(S.Inst);
  Value *Frozen = Builder.CreateFreeze(S.Other, "obf.const.freeze");
  tag(Frozen, Marker);
  Value *Bit = Builder.CreateAnd(Frozen, ConstantInt::get(Ty, 1),
                                 "obf.const.bit");
  tag(Bit, Marker);
  Value *LowBit = Builder.CreateTrunc(Bit, Builder.getInt1Ty(),
                                      "obf.const.lowbit");
  tag(LowBit, Marker);
  Value *Index = Builder.CreateZExt(LowBit, Builder.getInt32Ty(),
                                    "obf.const.index");
  tag(Index, Marker);
  Value *Left = loadShare(Builder, A, Index, Ty, Marker);
  Value *Right = loadShare(Builder, B, Index, Ty, Marker);
  Value *Decoded = Builder.CreateXor(Left, Right, "obf.const.decoded");
  tag(Decoded, Marker);
  S.Inst->setOperand(S.Operand, Decoded);
  S.Inst->setMetadata(GeneratedTag, Marker);
}

} // namespace

PreservedAnalyses ConstantEncodingPass::run(Module &M, ModuleAnalysisManager &AM) {
  (void)AM;
  if (SelectedValues.empty()) {
    if (Flag)
      reportObfuscationSkip("constenc", "module", M.getName(),
                            "no-selected-values");
    return PreservedAnalyses::all();
  }
  if (!Flag && !M.getGlobalVariable("llvm.global.annotations"))
    return PreservedAnalyses::all();

  SmallVector<TypedValue, 8> Values;
  for (const std::string &Text : SelectedValues) {
    TypedValue Parsed{};
    // The command-line parser already validated each item. Keep this guard
    // for callers that programmatically alter command-line option storage.
    if (!parseTypedValue(Text, Parsed)) {
      M.getContext().emitError("invalid -constenc-values item");
      return PreservedAnalyses::all();
    }
    Values.push_back(Parsed);
  }

  MDNode *Marker = MDNode::get(M.getContext(), {});
  bool Changed = false;
  for (Function &F : M) {
    if (!toObfuscate(Flag, &F, "constenc"))
      continue;
    if (F.getName().starts_with(".datadiv_decode") ||
        F.hasFnAttribute(Attribute::Naked) || F.isConvergent()) {
      reportObfuscationSkip("constenc", "function", F.getName(),
                            "generated-or-unsafe-function");
      continue;
    }
    bool UnsafeControlFlow = false;
    for (BasicBlock &BB : F) {
      if (BB.isEHPad()) {
        UnsafeControlFlow = true;
        break;
      }
      for (Instruction &I : BB) {
        if (I.getType()->isTokenTy()) {
          UnsafeControlFlow = true;
          break;
        }
        if (auto *Call = dyn_cast<CallBase>(&I)) {
          if (Call->isConvergent() ||
              Call->countOperandBundlesOfType(LLVMContext::OB_convergencectrl)) {
            UnsafeControlFlow = true;
            break;
          }
        }
      }
      if (UnsafeControlFlow)
        break;
    }
    if (UnsafeControlFlow) {
      reportObfuscationSkip("constenc", "function", F.getName(),
                            "unsupported-control-flow");
      continue;
    }

    SmallVector<Site, 64> Sites;
    for (BasicBlock &BB : F) {
      for (Instruction &I : BB) {
        if (!isEligibleInstruction(I))
          continue;
        for (unsigned Op = 0; Op < 2; ++Op) {
          auto *C = dyn_cast<ConstantInt>(I.getOperand(Op));
          Value *Other = I.getOperand(1 - Op);
          if (!C || isa<Constant>(Other) || !selected(*C, Values))
            continue;
          Sites.push_back({&I, Op, Other, C});
          break;
        }
        if (Sites.size() > static_cast<size_t>(MaxSites))
          break;
      }
      if (Sites.size() > static_cast<size_t>(MaxSites))
        break;
    }

    bool HitSiteLimit = Sites.size() > static_cast<size_t>(MaxSites);
    if (HitSiteLimit)
      Sites.pop_back();

    if (Sites.empty()) {
      reportObfuscationSkip("constenc", "function", F.getName(),
                            HitSiteLimit ? "site-limit" : "no-selected-sites");
      continue;
    }
    for (const Site &S : Sites)
      encodeSite(M, S, Marker);
    reportObfuscationEffect("constenc", "function", F.getName(), Sites.size());
    if (HitSiteLimit)
      reportObfuscationSkip("constenc", "function", F.getName(),
                            "site-limit");
    Changed = true;
  }
  return Changed ? PreservedAnalyses::none() : PreservedAnalyses::all();
}
