#include "llvm/Transforms/Obfuscation/CryptoUtils.h"
#include "gtest/gtest.h"

#include <algorithm>
#include <string>
#include <system_error>
#include <thread>
#include <vector>

using namespace llvm;

namespace {

constexpr char Seed[] = "00112233445566778899aabbccddeeff";

std::error_code failingEntropy(void *, size_t) {
  return std::make_error_code(std::errc::io_error);
}

TEST(CryptoUtilsTest, EntropyFailureStopsGeneration) {
#if GTEST_HAS_DEATH_TEST
  EXPECT_DEATH(
      {
        CryptoUtils Random(failingEntropy);
        char Byte;
        Random.get_bytes(&Byte, 1);
      },
      "entropy");
#endif
}

TEST(CryptoUtilsTest, ExplicitSeedValidation) {
  CryptoUtils Random;
  EXPECT_FALSE(Random.prng_seed("00112233445566778899aabbccddeef"));
  EXPECT_FALSE(Random.prng_seed("xx00112233445566778899aabbccddeeff"));
  EXPECT_FALSE(Random.prng_seed("00112233445566778899aabbccddeefg"));
  EXPECT_TRUE(Random.prng_seed(Seed));
  EXPECT_TRUE(Random.prng_seed(std::string("0x") + Seed));
}

TEST(CryptoUtilsTest, ExplicitSeedRepeatsStream) {
  CryptoUtils First;
  CryptoUtils Second;
  ASSERT_TRUE(First.prng_seed(Seed));
  ASSERT_TRUE(Second.prng_seed(Seed));
  std::string A(4096, '\0');
  std::string B(4096, '\0');
  First.get_bytes(A.data(), static_cast<int>(A.size()));
  Second.get_bytes(B.data(), static_cast<int>(B.size()));
  EXPECT_EQ(A, B);
}

TEST(CryptoUtilsTest, ReadAcrossPoolBoundaryFillsLastByte) {
  CryptoUtils OneRead;
  CryptoUtils TwoReads;
  ASSERT_TRUE(OneRead.prng_seed(Seed));
  ASSERT_TRUE(TwoReads.prng_seed(Seed));
  std::string Actual(CryptoUtils_POOL_SIZE + 1, '\0');
  std::string Expected(CryptoUtils_POOL_SIZE + 1, '\0');
  OneRead.get_bytes(Actual.data(), static_cast<int>(Actual.size()));
  TwoReads.get_bytes(Expected.data(), CryptoUtils_POOL_SIZE);
  TwoReads.get_bytes(Expected.data() + CryptoUtils_POOL_SIZE, 1);
  EXPECT_EQ(Actual, Expected);
}

TEST(CryptoUtilsTest, ConcurrentReadsAreWholeStreamChunks) {
  constexpr int Chunks = 16;
  constexpr int ChunkSize = 4096;
  CryptoUtils Parallel;
  CryptoUtils Serial;
  ASSERT_TRUE(Parallel.prng_seed(Seed));
  ASSERT_TRUE(Serial.prng_seed(Seed));

  std::vector<std::string> Actual(Chunks, std::string(ChunkSize, '\0'));
  std::vector<std::thread> Threads;
  Threads.reserve(Chunks);
  for (int I = 0; I < Chunks; ++I)
    Threads.emplace_back([&Parallel, &Actual, I] {
      Parallel.get_bytes(Actual[I].data(), ChunkSize);
    });
  for (std::thread &Thread : Threads)
    Thread.join();

  std::vector<std::string> Expected(Chunks, std::string(ChunkSize, '\0'));
  for (std::string &Chunk : Expected)
    Serial.get_bytes(Chunk.data(), ChunkSize);
  std::sort(Actual.begin(), Actual.end());
  std::sort(Expected.begin(), Expected.end());
  EXPECT_EQ(Actual, Expected);
}

} // namespace
