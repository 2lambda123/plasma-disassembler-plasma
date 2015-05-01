import lib.arch.x86.output
import lib.arch.x86.utils
import lib.arch.x86.process_ast

registered = [
    process_ast.fuse_inst_with_if,
    process_ast.search_local_vars,
    process_ast.search_canary_plt,
]
