"""Run no-account native startup with a kernel socket filter; no network/model calls.
All integration servers run in separate Node processes outside this native-only probe.
"""
import ctypes
import ctypes.util
import os
import sys

class Compare(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int), ('a', ctypes.c_uint64), ('b', ctypes.c_uint64)]

lib = ctypes.CDLL(ctypes.util.find_library('seccomp') or 'libseccomp.so.2', use_errno=True)
lib.seccomp_init.argtypes = [ctypes.c_uint32]
lib.seccomp_init.restype = ctypes.c_void_p
lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Compare)]
lib.seccomp_load.argtypes = [ctypes.c_void_p]
ctx = lib.seccomp_init(0x7fff0000)
assert ctx
for family in (2, 10):  # AF_INET / AF_INET6; allow local Unix IPC only
    cmp = Compare(0, 4, family, 0)  # SCMP_CMP_EQ
    assert lib.seccomp_rule_add_array(ctx, 0x50000 | 1, lib.seccomp_syscall_resolve_name(b'socket'), 1, ctypes.byref(cmp)) == 0
assert lib.seccomp_load(ctx) == 0
os.execv(sys.argv[1], sys.argv[1:])
