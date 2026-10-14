; Rust-style private byte arrays have no required trailing NUL. The descriptor
; globals match rustc's pointer plus little-endian length representation.
; The companion string_data_skips.ll covers conservative exclusions.

@ascii = private unnamed_addr constant [22 x i8] c"rust-ascii-secret-29db", align 1
@utf8 = private unnamed_addr constant [19 x i8] c"rust-utf8-\CE\B2-\E6\BC\A2\E5\AD\97", align 1
@nul = private unnamed_addr constant [20 x i8] c"rust-nul-secret\00tail", align 1
@bytes = private unnamed_addr constant [20 x i8] c"rust-byte-secret-\FF\00\80", align 1
@static_ref = private constant <{ ptr, [8 x i8] }> <{ ptr @ascii, [8 x i8] c"\16\00\00\00\00\00\00\00" }>, align 8
@const_ref = private constant <{ ptr, [8 x i8] }> <{ ptr @utf8, [8 x i8] c"\13\00\00\00\00\00\00\00" }>, align 8

define internal i32 @sum_bytes(ptr %data, i64 %len) {
entry:
  br label %loop

loop:
  %index = phi i64 [ 0, %entry ], [ %next, %body ]
  %sum = phi i32 [ 0, %entry ], [ %added, %body ]
  %done = icmp eq i64 %index, %len
  br i1 %done, label %exit, label %body

body:
  %address = getelementptr inbounds i8, ptr %data, i64 %index
  %byte = load volatile i8, ptr %address, align 1
  %wide = zext i8 %byte to i32
  %added = add i32 %sum, %wide
  %next = add i64 %index, 1
  br label %loop

exit:
  ret i32 %sum
}

define i32 @main() {
entry:
  %static_data = load ptr, ptr @static_ref, align 8
  %static_len_addr = getelementptr inbounds i8, ptr @static_ref, i64 8
  %static_len = load i64, ptr %static_len_addr, align 8
  %static_sum = call i32 @sum_bytes(ptr %static_data, i64 %static_len)
  %const_data = load ptr, ptr @const_ref, align 8
  %const_len_addr = getelementptr inbounds i8, ptr @const_ref, i64 8
  %const_len = load i64, ptr %const_len_addr, align 8
  %const_sum = call i32 @sum_bytes(ptr %const_data, i64 %const_len)
  %nul_sum = call i32 @sum_bytes(ptr @nul, i64 20)
  %byte_sum = call i32 @sum_bytes(ptr @bytes, i64 20)
  %part1 = add i32 %static_sum, %const_sum
  %part2 = add i32 %nul_sum, %byte_sum
  %total = add i32 %part1, %part2
  %correct = icmp eq i32 %total, 8595
  %status = select i1 %correct, i32 0, i32 1
  ret i32 %status
}
