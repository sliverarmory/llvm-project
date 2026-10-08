; Each use of an undef-dependent value may observe a different value. Both
; functions have a fixed result before substitution, despite their undef input.

define i32 @scalar_or_undef() noinline optnone {
entry:
  %result = or i32 -1, undef
  ret i32 %result
}

define <2 x i32> @vector_or_undef() noinline optnone {
entry:
  %result = or <2 x i32> <i32 -1, i32 -1>, <i32 undef, i32 undef>
  ret <2 x i32> %result
}
