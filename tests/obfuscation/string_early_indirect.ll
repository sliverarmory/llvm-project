@message = private constant [19 x i8] c"early-indirect-use\00"
@reader = external global ptr
@llvm.global_ctors = appending global [1 x { i32, ptr, ptr }] [
  { i32, ptr, ptr } { i32 0, ptr @early_ctor, ptr null }
]

define internal void @early_ctor() {
  %callee = load ptr, ptr @reader
  call void %callee()
  ret void
}

define ptr @get_message() {
  ret ptr @message
}
