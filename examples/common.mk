WORKSPACE_ROOT ?= $(abspath $(CURDIR)/../../..)
CLANG ?= $(WORKSPACE_ROOT)/build-llvm-project/bin/clang
CLANGXX ?= $(WORKSPACE_ROOT)/build-llvm-project/bin/clang++

SDKROOT ?= $(shell xcrun --show-sdk-path 2>/dev/null)
SYSROOT_FLAG := $(if $(SDKROOT),-isysroot $(SDKROOT),)

COMMON_WARN := -Wall -Wextra -Wpedantic
BASE_OPT := -O2
OBF_FLAGS := -mllvm -sobf -mllvm -sub -mllvm -split -mllvm -bcf -mllvm -fla
