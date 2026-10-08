set(LLVM_LINK_COMPONENTS
  Obfuscation
  Support
  )

add_llvm_unittest(ObfuscationTests
  CryptoUtilsTest.cpp
  )
