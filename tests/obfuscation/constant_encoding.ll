; Scalar widths, comparison and arithmetic, and poison/undef refinement.
define i8 @encode_i8(i8 %x) {
  %r = add i8 %x, -83
  ret i8 %r
}

define i16 @encode_i16(i16 %x) {
  %r = xor i16 %x, -26307
  ret i16 %r
}

define i32 @encode_i32(i32 %x) {
  %r = add nsw i32 %x, 1803373201
  ret i32 %r
}

define i64 @encode_i64(i64 %x) {
  %r = icmp ult i64 %x, 7339196651810267521
  %z = zext i1 %r to i64
  ret i64 %z
}

define i32 @unselected_value(i32 %x) {
  %r = add i32 %x, 11
  ret i32 %r
}

; Selecting 1 also selects the generated row-index mask unless generated
; decoder instructions are excluded on subsequent runs.
define i32 @encode_one(i32 %x) {
  %r = add i32 %x, 1
  ret i32 %r
}

define i32 @unsupported_vector(<2 x i32> %x) {
  %r = add <2 x i32> %x, <i32 1803373201, i32 1803373201>
  %z = extractelement <2 x i32> %r, i32 0
  ret i32 %z
}

define i32 @caller_undef() {
  %r = call i32 @encode_i32(i32 undef)
  ret i32 %r
}

define i32 @caller_poison() {
  %r = call i32 @encode_i32(i32 poison)
  ret i32 %r
}
