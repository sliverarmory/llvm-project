; ELF preinit entries run before even priority-zero global constructors. Their
; byte readers must stay plaintext, while unrelated data can still be encoded.

@early = private constant [19 x i8] c"rust-preinit-secret", align 1
@early_sub = private constant [16 x i8] c"rust-preinit-sub", align 1
@early_init = private constant [21 x i8] c"rust-zero-init-secret", align 1
@late = private constant [16 x i8] c"rust-late-secret", align 1
@early_desc = internal constant { ptr, i64 } { ptr @early, i64 19 }
@early_sub_desc = internal constant { ptr, i64 } { ptr @early_sub, i64 16 }
@early_init_desc = internal constant { ptr, i64 } { ptr @early_init, i64 21 }
@seen = global i8 0
@preinit = constant ptr @preinit_reader, section ".preinit_array", align 8
@preinit_sub = constant [1 x ptr] [ptr @preinit_sub_reader], section ".preinit_array.00001", align 8
@init_zero = constant ptr @init_zero_reader, section ".init_array.00000", align 8
@llvm.used = appending global [3 x ptr] [ptr @preinit, ptr @preinit_sub, ptr @init_zero], section "llvm.metadata"

define void @preinit_reader() {
entry:
  %ptr = load ptr, ptr @early_desc
  %byte = call i8 @read_first_byte(ptr %ptr)
  store i8 %byte, ptr @seen
  ret void
}

define void @preinit_sub_reader() {
entry:
  %ptr = load ptr, ptr @early_sub_desc
  %byte = call i8 @read_first_byte(ptr %ptr)
  store i8 %byte, ptr @seen
  ret void
}

define void @init_zero_reader() {
entry:
  %ptr = load ptr, ptr @early_init_desc
  %byte = call i8 @read_first_byte(ptr %ptr)
  store i8 %byte, ptr @seen
  ret void
}

define i8 @late_reader() {
entry:
  %byte = load i8, ptr @late
  ret i8 %byte
}

declare i8 @read_first_byte(ptr)
