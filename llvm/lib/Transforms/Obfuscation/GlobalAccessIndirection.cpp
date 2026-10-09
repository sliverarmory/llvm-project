//===- GlobalAccessIndirection.cpp - Selective global access indirection --===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "llvm/Transforms/Obfuscation/GlobalAccessIndirection.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/InlineAsm.h"
#include "llvm/IR/GlobalVariable.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/Metadata.h"
#include "llvm/IR/Module.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/TargetParser/Triple.h"
#include "llvm/Transforms/Obfuscation/Utils.h"
#include <cstdint>
#include <map>
#include <string>

using namespace llvm;

static cl::list<std::string> OnlyGlobals(
    "gai-only-globals", cl::CommaSeparated,
    cl::desc("Indirect accesses only to these exact LLVM global names"));

namespace {

static constexpr StringLiteral GeneratedTag = "obf.gai";

struct AccessSite {
  Instruction *Inst;
  unsigned PointerOperand;
};

static bool isSelected(StringRef Name) {
  for (const std::string &Selected : OnlyGlobals)
    if (Name == Selected)
      return true;
  return false;
}

static bool isRuntimeName(StringRef Name) {
  return Name.starts_with("llvm.") || Name.starts_with("__llvm") ||
         Name.starts_with("__clang") || Name.starts_with("__asan") ||
         Name.starts_with("__msan") || Name.starts_with("__tsan") ||
         Name.starts_with("__ubsan") || Name.starts_with("__cfi") ||
         Name.starts_with("OBJC_") || Name.starts_with("_OBJC_") ||
         Name.starts_with("__objc") || Name.starts_with(".objc");
}

// The empty assembly keeps its input and output in the same register. It is
// an identity operation at runtime, but LTO cannot fold the address of a
// constant slot through it. Restrict this to targets with a general-purpose
// register constraint that supports pointers.
static bool supportsOpaqueSlotAddress(const Module &M) {
  Triple::ArchType Arch = Triple(M.getTargetTriple()).getArch();
  return Arch == Triple::x86_64 || Arch == Triple::aarch64;
}

static Function *createPointerHelper(Module &M, GlobalVariable &GV,
                                     GlobalVariable &Slot, MDNode *Marker) {
  FunctionType *HelperType = FunctionType::get(GV.getType(), false);
  Function *Helper = Function::Create(
      HelperType, GlobalValue::InternalLinkage,
      (Twine(".obf.gai.get.") + GV.getName()).str(), M);
  Helper->addFnAttr(Attribute::NoInline);
  Helper->addFnAttr(Attribute::OptimizeNone);
  Helper->setMetadata(GeneratedTag, Marker);

  IRBuilder<> Builder(BasicBlock::Create(M.getContext(), "entry", Helper));
  FunctionType *AsmType =
      FunctionType::get(Slot.getType(), {Slot.getType()}, false);
  InlineAsm *Identity = InlineAsm::get(AsmType, "", "=r,0", false);
  Value *OpaqueSlot =
      Builder.CreateCall(Identity, {&Slot}, "obf.gai.slot.addr");
  LoadInst *Pointer =
      Builder.CreateLoad(GV.getType(), OpaqueSlot, "obf.gai.ptr");
  Pointer->setMetadata(GeneratedTag, Marker);
  Builder.CreateRet(Pointer);
  return Helper;
}

// A helper call is unsuitable for unusual calling conventions and for EH
// funclets, which require call-site operand bundles in their protected region.
static bool canCallPointerHelper(const Function &F) {
  return F.getCallingConv() == CallingConv::C &&
         !F.hasFnAttribute(Attribute::Naked) &&
         !F.hasFnAttribute("interrupt") && !F.isConvergent() &&
         !F.hasPersonalityFn();
}

// A whole-global decision avoids partially redirecting accesses when some
// use has semantics that this simple rewriter does not model.
static StringRef collectSites(GlobalVariable &GV,
                              SmallVectorImpl<AccessSite> &Sites,
                              bool EnabledByFlag) {
  if (GV.hasMetadata(GeneratedTag) || GV.getName().starts_with(".obf.gai."))
    return "generated";
  if (!GV.hasInitializer())
    return "no-initializer";
  if (!GV.hasLocalLinkage())
    return "non-local";
  if (GV.isConstant())
    return "constant";
  if (GV.getAddressSpace() != 0)
    return "address-space";
  if (GV.isThreadLocal())
    return "thread-local";
  if (GV.hasComdat())
    return "comdat";
  if (GV.hasSection())
    return "section";
  if (GV.isExternallyInitialized())
    return "external-init";
  if (GV.hasMetadata() || isRuntimeName(GV.getName()))
    return "metadata";
  Type *ValueType = GV.getValueType();
  if (!ValueType->isIntegerTy(8) && !ValueType->isIntegerTy(16) &&
      !ValueType->isIntegerTy(32) && !ValueType->isIntegerTy(64))
    return "not-integer";

  for (Use &U : GV.uses()) {
    auto *I = dyn_cast<Instruction>(U.getUser());
    if (!I)
      return "unsafe-use";
    if (auto *LI = dyn_cast<LoadInst>(I)) {
      if (LI->getPointerOperand() != &GV || LI->isAtomic() || LI->isVolatile())
        return "unsafe-use";
      // This metadata describes the original pointer SSA value; keeping it
      // after replacing that value would violate the LangRef rule.
      if (LI->getMetadata(LLVMContext::MD_invariant_group))
        return "invariant-group";
      if (!toObfuscate(EnabledByFlag, LI->getFunction(), "gai"))
        return "function-not-selected";
      if (!canCallPointerHelper(*LI->getFunction()))
        return "function-abi";
      Sites.push_back({LI, LoadInst::getPointerOperandIndex()});
      continue;
    }
    if (auto *SI = dyn_cast<StoreInst>(I)) {
      if (SI->getPointerOperand() != &GV || SI->isAtomic() || SI->isVolatile())
        return "unsafe-use";
      if (SI->getMetadata(LLVMContext::MD_invariant_group))
        return "invariant-group";
      if (!toObfuscate(EnabledByFlag, SI->getFunction(), "gai"))
        return "function-not-selected";
      if (!canCallPointerHelper(*SI->getFunction()))
        return "function-abi";
      Sites.push_back({SI, StoreInst::getPointerOperandIndex()});
      continue;
    }
    return "unsafe-use";
  }
  return Sites.empty() ? "no-accesses" : StringRef();
}

} // namespace

PreservedAnalyses GlobalAccessIndirectionPass::run(Module &M,
                                                    ModuleAnalysisManager &AM) {
  (void)AM;
  if (!Flag && OnlyGlobals.empty())
    return PreservedAnalyses::all();
  if (OnlyGlobals.empty()) {
    M.getContext().emitError("-gai requires -gai-only-globals=<name[,name...]>");
    return PreservedAnalyses::all();
  }

  // An unknown triple is useful for target-independent opt IR tests. A known
  // unsupported triple must not silently emit an LTO-foldable transform.
  if (!M.getTargetTriple().empty() && !supportsOpaqueSlotAddress(M)) {
    M.getContext().emitError(
        "-gai currently supports only x86_64 and AArch64 targets");
    return PreservedAnalyses::all();
  }

  bool Changed = false;
  // The pass creates new globals, so save the original list before mutation.
  SmallVector<GlobalVariable *, 16> Globals;
  for (GlobalVariable &GV : M.globals())
    Globals.push_back(&GV);

  for (GlobalVariable *GV : Globals) {
    if (!isSelected(GV->getName())) {
      // Report plausible local scalar candidates, but avoid diagnostics for
      // every unrelated runtime or metadata global in a normal module.
      if (GV->hasInitializer() && GV->hasLocalLinkage() &&
          !GV->isConstant() && GV->getAddressSpace() == 0 &&
          GV->getValueType()->isIntegerTy() && !GV->hasMetadata() &&
          !isRuntimeName(GV->getName()))
        reportObfuscationSkip("gai", "global", GV->getName(),
                              "not-selected");
      continue;
    }

    SmallVector<AccessSite, 16> Sites;
    StringRef Reason = collectSites(*GV, Sites, Flag);
    if (!Reason.empty()) {
      reportObfuscationSkip("gai", "global", GV->getName(), Reason);
      continue;
    }

    auto *Slot = new GlobalVariable(
        M, GV->getType(), /*isConstant=*/true, GlobalValue::PrivateLinkage,
        GV, (Twine(".obf.gai.") + GV->getName()).str());
    MDNode *Marker = MDNode::get(M.getContext(), {});
    Slot->setMetadata(GeneratedTag, Marker);
    GV->setMetadata(GeneratedTag, Marker);

    Function *Helper = supportsOpaqueSlotAddress(M)
                           ? createPointerHelper(M, *GV, *Slot, Marker)
                           : nullptr;

    std::map<std::string, uint64_t> FunctionSites;
    for (const AccessSite &Site : Sites) {
      IRBuilder<> Builder(Site.Inst);
      Value *Pointer;
      if (Helper) {
        CallInst *Call = Builder.CreateCall(Helper, {}, "obf.gai.ptr");
        Call->setDebugLoc(Site.Inst->getDebugLoc());
        Call->setMetadata(GeneratedTag, Marker);
        Pointer = Call;
      } else {
        LoadInst *Load = Builder.CreateLoad(GV->getType(), Slot, "obf.gai.ptr");
        Load->setDebugLoc(Site.Inst->getDebugLoc());
        Load->setMetadata(GeneratedTag, Marker);
        Pointer = Load;
      }
      Site.Inst->setOperand(Site.PointerOperand, Pointer);
      ++FunctionSites[Site.Inst->getFunction()->getName().str()];
    }
    reportObfuscationEffect("gai", "global", GV->getName(), Sites.size());
    for (const auto &[Name, Count] : FunctionSites)
      reportObfuscationEffect("gai", "function", Name, Count);
    Changed = true;
  }

  for (const std::string &Selected : OnlyGlobals)
    if (!M.getNamedGlobal(Selected))
      reportObfuscationSkip("gai", "global", Selected, "not-found");

  return Changed ? PreservedAnalyses::none() : PreservedAnalyses::all();
}
