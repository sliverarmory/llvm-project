; Selected byte arrays with unsafe ownership, metadata, or early readers stay
; plaintext and report why. This is an opt-only fixture: section spellings
; differ between Mach-O, ELF, and COFF.

@exported = constant [18 x i8] c"rust-exported-4a6d"
@weak = weak_odr constant [14 x i8] c"rust-weak-2d7c"
@early = private constant [15 x i8] c"rust-early-7f4a"
@metadata_data = private constant [18 x i8] c"rust-metadata-42e1"
@orphan = private constant [16 x i8] c"rust-orphan-87b3"
@mixed = private constant [15 x i8] c"rust-mixed-103a"
@descriptor_data = private constant [20 x i8] c"rust-descriptor-6c2e"
@descriptor_public = constant { ptr, i64 } { ptr @descriptor_data, i64 20 }
@sectioned = private constant [17 x i8] c"rust-section-9b1f", section ".rust-custom"
@ptrint_data = private constant [16 x i8] c"rust-ptrint-0d3c"
@ptrint_address = private constant i64 ptrtoint (ptr @ptrint_data to i64)
@llvm.used = appending global [1 x ptr] [ptr @metadata_data], section "llvm.metadata"
@llvm.global_ctors = appending global [1 x { i32, ptr, ptr }] [{ i32, ptr, ptr } { i32 0, ptr @early_reader, ptr null }]

define void @early_reader() {
entry:
  %first = load volatile i8, ptr @early, align 1
  ret void
}

define i8 @selected() {
entry:
  %first = load i8, ptr @mixed, align 1
  ret i8 %first
}

define i8 @unselected() {
entry:
  %first = load i8, ptr @mixed, align 1
  ret i8 %first
}
