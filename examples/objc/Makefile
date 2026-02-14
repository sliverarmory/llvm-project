include ../common.mk

TARGET := objc_example
SRC := main.m
BASE_BIN := $(TARGET).base
OBF_BIN := $(TARGET).obf
BASE_LL := $(TARGET).base.ll
OBF_LL := $(TARGET).obf.ll
OBJC_FLAGS := -fobjc-arc
OBJC_LINK := -framework Foundation
OBF_FLAGS := -mllvm -sub -mllvm -split -mllvm -bcf -mllvm -fla

.PHONY: all check clean

all: $(BASE_BIN) $(OBF_BIN)

$(BASE_BIN): $(SRC)
	$(CLANG) $(SYSROOT_FLAG) $(COMMON_WARN) $(BASE_OPT) $(OBJC_FLAGS) $< \
	$(OBJC_LINK) -o $@

$(OBF_BIN): $(SRC)
	$(CLANG) $(SYSROOT_FLAG) $(COMMON_WARN) $(BASE_OPT) $(OBJC_FLAGS) \
	$(OBF_FLAGS) $< $(OBJC_LINK) -o $@

$(BASE_LL): $(SRC)
	$(CLANG) $(SYSROOT_FLAG) $(BASE_OPT) $(OBJC_FLAGS) -S -emit-llvm $< -o $@

$(OBF_LL): $(SRC)
	$(CLANG) $(SYSROOT_FLAG) $(BASE_OPT) $(OBJC_FLAGS) $(OBF_FLAGS) \
	-S -emit-llvm $< -o $@

check: all $(BASE_LL) $(OBF_LL)
	./$(BASE_BIN) > $(BASE_BIN).out
	./$(OBF_BIN) > $(OBF_BIN).out
	diff -u $(BASE_BIN).out $(OBF_BIN).out
	@if ! grep -E '@x[[:alnum:]_.]* =|@y[[:alnum:]_.]* =' $(OBF_LL) >/dev/null; then \
		echo "FAIL: expected bogus-control-flow markers not found in $(OBF_LL)"; \
		exit 1; \
	fi
	@base_br=$$(grep -c '^[[:space:]]*br ' $(BASE_LL) || true); \
	obf_br=$$(grep -c '^[[:space:]]*br ' $(OBF_LL) || true); \
	if [ "$$obf_br" -le "$$base_br" ]; then \
		echo "FAIL: obfuscated IR branch count did not grow ($$base_br -> $$obf_br)"; \
		exit 1; \
	fi
	@echo "PASS: Objective-C example output matches and obfuscation checks passed."

clean:
	rm -f $(TARGET).base $(TARGET).obf $(TARGET).base.ll $(TARGET).obf.ll \
	$(TARGET).base.out $(TARGET).obf.out
