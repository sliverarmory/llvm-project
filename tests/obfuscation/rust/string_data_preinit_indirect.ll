; An unresolved call from preinit can reach any byte array before decoding.

@indirect_data = private constant [20 x i8] c"rust-indirect-secret", align 1
@target_pointer = private global ptr @target
@preinit = constant ptr @preinit_caller, section ".preinit_array", align 8
@llvm.used = appending global [1 x ptr] [ptr @preinit], section "llvm.metadata"

define void @preinit_caller() {
entry:
  %callee = load ptr, ptr @target_pointer
  call void %callee()
  ret void
}

define void @target() {
entry:
  ret void
}

define i8 @read_data() {
entry:
  %byte = load i8, ptr @indirect_data
  ret i8 %byte
}
