; Exact name selection, safe direct accesses, and conservative exclusions.
@selected_value = internal global i32 13
@selected_value_peer = internal global i32 17
@function_filtered = internal global i32 19
@tls_value = internal thread_local global i32 23
@weak_value = weak global i32 29
@constant_value = internal constant i32 31
@volatile_value = internal global i32 37
@atomic_value = internal global i32 41
@escaped_value = internal global i32 43
@section_value = internal global i32 47, section ".obf_test"
@metadata_value = internal global i32 53, !obf.fixture !0
@addrspace_value = internal addrspace(1) global i32 59
@external_init_value = internal externally_initialized global i32 61
@nonlocal_value = global i32 67
@float_value = internal global float 7.000000e+00
@personality_value = internal global i32 71
@special_cc_value = internal global i32 73
@invariant_group_value = internal global i32 79

declare void @escape(ptr)
declare i32 @__gxx_personality_v0(...)

define i32 @selected_user(i32 %x) {
  %old = load i32, ptr @selected_value
  %new = add i32 %old, %x
  store i32 %new, ptr @selected_value
  ret i32 %new
}

define i32 @peer_user(i32 %x) {
  %old = load i32, ptr @selected_value_peer
  %new = add i32 %old, %x
  store i32 %new, ptr @selected_value_peer
  ret i32 %new
}

define i32 @blocked_user(i32 %x) {
  %old = load i32, ptr @function_filtered
  %new = add i32 %old, %x
  store i32 %new, ptr @function_filtered
  ret i32 %new
}

define i32 @read_tls() {
  %v = load i32, ptr @tls_value
  ret i32 %v
}

define i32 @read_weak() {
  %v = load i32, ptr @weak_value
  ret i32 %v
}

define i32 @read_constant() {
  %v = load i32, ptr @constant_value
  ret i32 %v
}

define i32 @read_volatile() {
  %v = load volatile i32, ptr @volatile_value
  ret i32 %v
}

define i32 @read_atomic() {
  %v = load atomic i32, ptr @atomic_value seq_cst, align 4
  ret i32 %v
}

define void @escape_global() {
  call void @escape(ptr @escaped_value)
  ret void
}

define i32 @read_section() {
  %v = load i32, ptr @section_value
  ret i32 %v
}

define i32 @read_metadata() {
  %v = load i32, ptr @metadata_value
  ret i32 %v
}

define i32 @read_addrspace() {
  %v = load i32, ptr addrspace(1) @addrspace_value
  ret i32 %v
}

define i32 @read_external_init() {
  %v = load i32, ptr @external_init_value
  ret i32 %v
}

define i32 @read_nonlocal() {
  %v = load i32, ptr @nonlocal_value
  ret i32 %v
}

define float @read_float() {
  %v = load float, ptr @float_value
  ret float %v
}

define i32 @personality_user() personality ptr @__gxx_personality_v0 {
  %v = load i32, ptr @personality_value
  ret i32 %v
}

define fastcc i32 @special_cc_user() {
  %v = load i32, ptr @special_cc_value
  ret i32 %v
}

define i32 @invariant_group_user() {
  %v = load i32, ptr @invariant_group_value, !invariant.group !1
  ret i32 %v
}

!0 = !{}
!1 = !{}
