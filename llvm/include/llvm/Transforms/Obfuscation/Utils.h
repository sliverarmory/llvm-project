#ifndef _OBFUSCATION_UTILS_H_
#define _OBFUSCATION_UTILS_H_

#include "llvm/ADT/StringRef.h"
#include "llvm/IR/BasicBlock.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/Instructions.h"
#include "llvm/Transforms/Utils/Local.h" // For DemoteRegToStack and DemotePHIToStack
#include <string>

void fixStack(llvm::Function *f);
std::string readAnnotate(llvm::Function *f);
bool toObfuscate(bool flag, llvm::Function *f, std::string const &attribute);
bool hasMustTailCall(const llvm::BasicBlock &BB);
void reportObfuscationSkip(llvm::StringRef Pass, llvm::StringRef Kind,
                           llvm::StringRef Symbol, llvm::StringRef Reason);

#endif
