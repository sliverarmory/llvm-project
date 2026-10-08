; Convergence tokens have strict placement and cycle rules. BCF must leave this
; function alone while still transforming the ordinary function below.
declare token @llvm.experimental.convergence.entry() convergent
declare void @convergent_op() convergent

define i32 @convergent_target(i32 %value) convergent {
entry:
  %token = call token @llvm.experimental.convergence.entry()
  %positive = icmp sgt i32 %value, 0
  br i1 %positive, label %then, label %else

then:
  call void @convergent_op() [ "convergencectrl"(token %token) ]
  %added = add i32 %value, 1
  ret i32 %added

else:
  call void @convergent_op() [ "convergencectrl"(token %token) ]
  %negated = sub i32 0, %value
  ret i32 %negated
}

define i32 @ordinary_target(i32 %value) {
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
