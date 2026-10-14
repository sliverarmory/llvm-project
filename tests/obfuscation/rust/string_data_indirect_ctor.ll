; An indirect call in a priority-zero constructor can reach a byte reader.
; The pass cannot prove otherwise, so it conservatively skips the allocation.

@indirect_data = private constant [20 x i8] c"rust-indirect-3d8b2f"
@target_pointer = private global ptr @target
@llvm.global_ctors = appending global [1 x { i32, ptr, ptr }] [{ i32, ptr, ptr } { i32 0, ptr @early_caller, ptr null }]

define void @early_caller() {
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
