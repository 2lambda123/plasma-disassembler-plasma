#!/bin/python3
#
# Reverse : reverse engineering for x86 binaries
# Copyright (C) 2015    Joel
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.    If not, see <http://www.gnu.org/licenses/>.
#

from lib.utils import invert_cond, is_call, is_uncond_jump, BRANCH_NEXT
from lib.colors import pick_color, addr_color, color, color_keyword
from lib.output import (print_block, print_if_cond, print_cmp_jump_commented,
        print_comment, print_no_end, print_tabbed, print_tabbed_no_end,
        ASSIGNMENT_OPS)
from capstone.x86 import (X86_INS_CMP, X86_INS_MOV, X86_INS_TEST, X86_OP_IMM,
        X86_OP_INVALID, X86_OP_REG, X86_REG_EBP, X86_REG_RBP)

gph = None
binary = None
dis = None
nocomment = False


local_vars_idx = {}
local_vars_size = []
local_vars_name = []
vars_counter = 1

# If an address of a cmp is here, it means that we have fused 
# with an if, so don't print this instruction.
cmp_fused = set()

FUSE_OPS = set(ASSIGNMENT_OPS)
FUSE_OPS.add(X86_INS_CMP)


class Ast_Branch:
    def __init__(self):
        self.nodes = []

    def add(self, node):
        if isinstance(node, Ast_Branch):
            self.nodes += node.nodes
        else:
            self.nodes.append(node)

    def print(self, tab=0):
        for n in self.nodes:
            if isinstance(n, list):
                print_block(n, tab)
            else: # ast
                n.print(tab)


class Ast_IfGoto:
    def __init__(self, orig_jump, cond_id, addr_jump):
        self.orig_jump = orig_jump
        self.cond_id = cond_id
        self.addr_jump = addr_jump
        self.cmp_inst = None

    def print(self, tab=0):
        print_cmp_jump_commented(self.cmp_inst, self.orig_jump, tab)
        print_tabbed_no_end(color_keyword("if "), tab)
        print_if_cond(self.cmp_inst, self.cond_id)
        print_no_end(color_keyword("  goto "))
        print_addr(self.addr_jump)


class Ast_AndIf:
    def __init__(self, orig_jump, cond_id):
        self.orig_jump = orig_jump
        self.cond_id = cond_id
        self.cmp_inst = None

    def print(self, tab=0):
        print_cmp_jump_commented(self.cmp_inst, self.orig_jump, tab)
        print_tabbed_no_end(color_keyword("and ") + color_keyword("if "), tab)
        print_if_cond(self.cmp_inst, self.cond_id)
        print()


class Ast_Ifelse:
    def __init__(self, jump_inst, br_next_jump, br_next):
        self.jump_inst = jump_inst
        self.br_next = br_next
        self.br_next_jump = br_next_jump
        self.cmp_inst = None

    def print(self, tab=0, print_else_keyword=False):

        #
        # if cond {
        # } else {
        #   ...
        # }
        #
        # become
        #
        # if !cond {
        #   ...
        # }
        #

        br_next = self.br_next
        br_next_jump = self.br_next_jump
        inv_if = False

        if len(self.br_next.nodes) == 0:
            br_next, br_next_jump = br_next_jump, br_next
            inv_if = True
            
        print_cmp_jump_commented(self.cmp_inst, self.jump_inst, tab)

        if print_else_keyword:
            print_tabbed_no_end(color_keyword("else if "), tab)
        else:
            print_tabbed_no_end(color_keyword("if "), tab)

        # jump_inst is the condition to go to the else-part
        if inv_if:
            print_if_cond(self.cmp_inst, self.jump_inst.id)
        else:
            print_if_cond(self.cmp_inst, invert_cond(self.jump_inst.id))

        print(" {")

        # if-part
        br_next.print(tab+1)

        # else-part
        if len(br_next_jump.nodes) > 0:
            print_tabbed_no_end("} ", tab)
            
            # 
            # if {
            #   ...
            # } else {
            #   if {
            #     ...
            #   }
            # }
            #
            # become :
            #
            # if {
            #   ...
            # }
            # else if {
            #   ...
            # }
            #

            br = br_next_jump

            if len(br.nodes) == 1 and isinstance(br.nodes[0], Ast_Ifelse):
                print()
                br.nodes[0].print(tab, True)
                return

            if len(br.nodes) == 2 and isinstance(br.nodes[0], list) and \
                  len(br.nodes[0]) == 1 and br.nodes[0][0].id == X86_INS_CMP and \
                  isinstance(br.nodes[1], Ast_Ifelse):
                print()
                br.nodes[1].print(tab, True)
                return

            print(color_keyword("else ") + "{")
            br.print(tab+1)

        print_tabbed("}", tab)


class Ast_Jmp:
    def __init__(self, addr):
        self.addr_jump = addr

    def print(self, tab=0):
        print_tabbed_no_end("jmp ", tab)
        print_addr(self.addr_jump)


def print_addr(addr):
    c = addr_color.get(addr)
    print(hex(addr) if c is None else color(hex(addr), c))


class Ast_Loop:
    def __init__(self):
        self.branch = Ast_Branch()
        self.epilog = None
        self.is_infinite = False

    def add(self, node):
        self.branch.add(node)

    def set_epilog(self, epilog):
        self.epilog = epilog

    def set_infinite(self, v):
        self.is_infinite = v

    def set_branch(self, b):
        self.branch = b

    def print(self, tab=0):
        if self.is_infinite:
            print_tabbed(color_keyword("infiniteloop") + " {", tab)
        else:
            print_tabbed(color_keyword("loop") + " {", tab)
        self.branch.print(tab+1)
        print_tabbed("}", tab)
        if self.epilog != None:
            self.epilog.print(tab)


class Ast_Comment:
    def __init__(self, text):
        self.text = text

    def print(self, tab=0):
        if not nocomment:
            print_comment("# " + self.text, tab)


# Functions for processing ast

def assign_colors(ast):
    if isinstance(ast, Ast_Branch):
        for n in ast.nodes:
            if isinstance(n, list):
                if is_uncond_jump(n[0]) and n[0].operands[0].type == X86_OP_IMM:
                    nxt = gph.link_out[n[0].address][BRANCH_NEXT]
                    pick_color(nxt)
            else: # ast
                assign_colors(n)

    elif isinstance(ast, Ast_IfGoto) or isinstance(ast, Ast_Jmp):
        pick_color(ast.addr_jump)

    elif isinstance(ast, Ast_Ifelse):
        assign_colors(ast.br_next_jump)
        assign_colors(ast.br_next)

    elif isinstance(ast, Ast_Loop):
        assign_colors(ast.branch)
        if ast.epilog != None:
            assign_colors(ast.epilog)


def fuse_cmp_if(ast):
    if isinstance(ast, Ast_Branch):
        types_ast = (Ast_Ifelse, Ast_IfGoto, Ast_AndIf)
        for i, n in enumerate(ast.nodes):
            if isinstance(n, list):
                if ((n[-1].id in FUSE_OPS or (n[-1].id == X86_INS_TEST and
                    all(op.type == X86_OP_REG for op in n[-1].operands) and
                    len(set(op.value.reg for op in n[-1].operands)) == 1))
                    and i+1 < len(ast.nodes)
                            and isinstance(ast.nodes[i+1], types_ast)):
                    ast.nodes[i+1].cmp_inst = n[-1]
                    cmp_fused.add(n[-1].address)
            else: # ast
                fuse_cmp_if(n)


    elif isinstance(ast, Ast_Ifelse):
        fuse_cmp_if(ast.br_next)
        fuse_cmp_if(ast.br_next_jump)

    elif isinstance(ast, Ast_Loop):
        fuse_cmp_if(ast.branch)
        if ast.epilog != None:
            fuse_cmp_if(ast.epilog)


def search_local_vars(ast):
    def inv(n):
        return n == X86_OP_INVALID

    def save_vars(i):
        global vars_counter
        for op in inst.operands:
            mm = op.mem
            if not inv(mm.base) and mm.disp != 0 \
                    and inv(mm.segment) and inv(mm.index) \
                    and (mm.base == X86_REG_RBP or mm.base == X86_REG_EBP):
                if mm.disp not in local_vars_idx:
                    local_vars_idx[mm.disp] = len(local_vars_name)
                    local_vars_name.append("var%d" % vars_counter)
                    local_vars_size.append(op.size)
                    vars_counter += 1


    if isinstance(ast, Ast_Branch):
        for i, n in enumerate(ast.nodes):
            if isinstance(n, list):
                for inst in n:
                    save_vars(inst)
            else: # ast
                search_local_vars(n)

    elif isinstance(ast, Ast_Ifelse):
        if ast.cmp_inst != None:
            save_vars(ast.cmp_inst)
        search_local_vars(ast.br_next)
        search_local_vars(ast.br_next_jump)

    elif isinstance(ast, Ast_IfGoto):
        if ast.cmp_inst != None:
            save_vars(ast.cmp_inst)

    elif isinstance(ast, Ast_Loop):
        search_local_vars(ast.branch)
        if ast.epilog != None:
            search_local_vars(ast.epilog)


def search_canary_plt():
    def inv(n):
        return n == X86_OP_INVALID

    fname = "__stack_chk_fail@plt"
    if fname not in binary.symbols:
        return

    faddr = binary.symbols[fname]

    k = 0
    for idx in dis.code_idx:
        i = dis.code[idx]
        if is_call(i):
            op = i.operands[0]
            if op.type == X86_OP_IMM and op.value.imm == faddr:
                # Try to get VAR
                #
                # rax = VAR # mov rax, qword ptr [rbp - 8]
                # xor rax, [fs + 40]
                # je 0x400714
                # if != {
                #     call 0x4004f0 <__stack_chk_fail@plt>
                # }
                #

                kk = k - 1
                while kk > 0 and kk > k - 4:
                    inst = dis.code[dis.code_idx[kk]]

                    if inst.id == X86_INS_MOV:
                        mm = inst.operands[1].mem
                        if mm.disp != 0  and inv(mm.segment) and inv(mm.index) and \
                                mm.base in [X86_REG_RBP, X86_REG_EBP] and \
                                mm.disp in local_vars_idx:
                            local_vars_name[local_vars_idx[mm.disp]] += "_canary"
                            break

                    kk -= 1

                break

        k += 1
