include ../common.mk

TARGET := cpp_example
SRC := main.cpp
BASE_BIN := $(TARGET).base
OBF_BIN := $(TARGET).obf
BASE_LL := $(TARGET).base.ll
OBF_LL := $(TARGET).obf.ll
SECRET := CPP_EXAMPLE_SECRET_MARKER

.PHONY: all check clean

all: $(BASE_BIN) $(OBF_BIN)

$(BASE_BIN): $(SRC)
	$(CLANGXX) $(SYSROOT_FLAG) $(COMMON_WARN) $(BASE_OPT) $< -o $@

$(OBF_BIN): $(SRC)
	$(CLANGXX) $(SYSROOT_FLAG) $(COMMON_WARN) $(BASE_OPT) $(OBF_FLAGS) $< -o $@

$(BASE_LL): $(SRC)
	$(CLANGXX) $(SYSROOT_FLAG) $(BASE_OPT) -S -emit-llvm $< -o $@

$(OBF_LL): $(SRC)
	$(CLANGXX) $(SYSROOT_FLAG) $(BASE_OPT) $(OBF_FLAGS) -S -emit-llvm $< -o $@

check: all $(BASE_LL) $(OBF_LL)
	./$(BASE_BIN) > $(BASE_BIN).out
	./$(OBF_BIN) > $(OBF_BIN).out
	diff -u $(BASE_BIN).out $(OBF_BIN).out
	@if strings ./$(OBF_BIN) | grep -F "$(SECRET)" >/dev/null; then \
		echo "FAIL: plaintext marker found in $(OBF_BIN)"; \
		exit 1; \
	fi
	@base_br=$$(grep -c '^[[:space:]]*br ' $(BASE_LL) || true); \
	obf_br=$$(grep -c '^[[:space:]]*br ' $(OBF_LL) || true); \
	if [ "$$obf_br" -le "$$base_br" ]; then \
		echo "FAIL: obfuscated IR branch count did not grow ($$base_br -> $$obf_br)"; \
		exit 1; \
	fi
	@echo "PASS: C++ example output matches and obfuscation checks passed."

clean:
	rm -f $(TARGET).base $(TARGET).obf $(TARGET).base.ll $(TARGET).obf.ll \
	$(TARGET).base.out $(TARGET).obf.out
