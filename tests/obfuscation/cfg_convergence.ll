; CFG obfuscation must leave convergence tokens and convergent calls in their
; original control flow. Split must also keep entry tokens in the entry block.
declare token @llvm.experimental.convergence.entry() convergent
declare token @llvm.experimental.convergence.loop() convergent
declare i32 @convergent_op(i32) convergent
declare void @may_throw()
declare i32 @__gxx_personality_v0(...)

; The convergent call is in a protected EH block, while entry and normal are
; otherwise split candidates. A function-wide preflight must still reject it.
define i32 @convergent_eh_cleanup(i32 %value) noinline optnone personality ptr @__gxx_personality_v0 {
entry:
  %base = add i32 %value, 1
  invoke void @may_throw()
    to label %normal unwind label %cleanup

normal:
  %result = add i32 %base, 2
  ret i32 %result

cleanup:
  %pad = landingpad { ptr, i32 } cleanup
  %unused = call i32 @convergent_op(i32 %base)
  resume { ptr, i32 } %pad
}

define i32 @controlled_loop(i32 %value) convergent noinline optnone {
entry:
  %slot = alloca i32
  %entry_token = call token @llvm.experimental.convergence.entry()
  br label %header

header:
  %index = phi i32 [ 0, %entry ], [ %next, %body ]
  %loop_token = call token @llvm.experimental.convergence.loop() [ "convergencectrl"(token %entry_token) ]
  %continue = icmp slt i32 %index, 2
  br i1 %continue, label %body, label %exit

body:
  %result = call i32 @convergent_op(i32 %value) [ "convergencectrl"(token %loop_token) ]
  %next = add nsw i32 %index, 1
  br label %header

exit:
  ret i32 %index
}

define i32 @uncontrolled_target(i32 %value) noinline optnone {
entry:
  %positive = icmp sgt i32 %value, 0
  br i1 %positive, label %then, label %else

then:
  %result = call i32 @convergent_op(i32 %value)
  ret i32 %result

else:
  %negated = sub i32 0, %value
  ret i32 %negated
}

define i32 @convergent_attribute_only(i32 %value) convergent noinline optnone {
entry:
  %positive = icmp sgt i32 %value, 0
  br i1 %positive, label %then, label %else

then:
  %added = add i32 %value, 1
  ret i32 %added

else:
  %negated = sub i32 0, %value
  ret i32 %negated
}

define i32 @ordinary_target(i32 %value) noinline optnone {
entry:
  %positive = icmp sgt i32 %value, 0
  br i1 %positive, label %then, label %else

then:
  %added = add i32 %value, 1
  ret i32 %added

else:
  %negated = sub i32 0, %value
  ret i32 %negated
}
