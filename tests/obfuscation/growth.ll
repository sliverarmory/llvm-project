; The named operations make dead originals visible after substitution. The
; branches give BCF several eligible blocks to exercise a per-function cap.

define i32 @sub_target(i32 %a, i32 %b) noinline optnone {
entry:
  %original_add = add i32 %a, %b
  %original_sub = sub i32 %original_add, %b
  %original_mul = mul i32 %original_sub, %a
  %original_and = and i32 %original_mul, %b
  ret i32 %original_and
}

define i32 @bcf_target(i32 %a) noinline optnone {
entry:
  %is_zero = icmp eq i32 %a, 0
  br i1 %is_zero, label %zero, label %other

zero:
  ret i32 7

other:
  ret i32 9
}

define i32 @main() noinline optnone {
entry:
  %sub_result = call i32 @sub_target(i32 8, i32 3)
  %bcf_result = call i32 @bcf_target(i32 0)
  %sub_ok = icmp eq i32 %sub_result, 0
  %bcf_ok = icmp eq i32 %bcf_result, 7
  %both_ok = and i1 %sub_ok, %bcf_ok
  %failed = xor i1 %both_ok, true
  %exit_code = zext i1 %failed to i32
  ret i32 %exit_code
}
