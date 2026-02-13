// LLVM include
#include "llvm/IR/PassManager.h"
#include "llvm/Pass.h"

namespace llvm {
      class StringObfuscationPass : public PassInfoMixin<StringObfuscationPass> {
      public:
            explicit StringObfuscationPass(bool Flag = false) : Flag(Flag) {}
            PreservedAnalyses run(Module &M, ModuleAnalysisManager &AM);

      private:
            bool Flag = false;
      };

      Pass* createStringObfuscation(bool flag);
}
