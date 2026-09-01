#!/usr/bin/env python3
"""Check pretty-printed IR for SSA / dominance violations.

Usage:
    python3 check_ssa.py out.txt [function-tag ...]

For every function (or just the named ones): builds the CFG (excluding %bom
fallthrough edges), computes dominators, and verifies that every register use
is dominated by its definition. Definitions are instructions with a `d` field,
block params, and function arguments (0v0 .. 0v<arity-1> at entry). Also flags
registers defined more than once (not SSA) and uses of never-defined registers.

Exit status: 0 if clean, 1 if any violation found.
"""

import re
import sys
from collections import deque

import visualize_ir as vz

REG = re.compile(r'^0v[0-9a-zA-Z~-]+$')
B64 = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-~'


def reg_name(i):
    s = ''
    while True:
        s = B64[i % 64] + s
        i //= 64
        if i == 0:
            return '0v' + s


def atoms(v):
    if isinstance(v, dict):
        for p in v.get('pos', []):
            yield from atoms(p)
        for k in v['order']:
            yield from atoms(v['fields'][k])
    elif isinstance(v, list):
        for p in v:
            yield from atoms(p)
    else:
        yield v


def check_function(f):
    """Return (violations, multi_def_regs, n_reachable).

    violations: [(block, instr_index, op, field, reg, kind)] where kind is
    'undefined' (no def anywhere) or 'not-dominated'.
    """
    blocks = f['blocks']
    succ = {b: [] for b in blocks}
    for b, blk in blocks.items():
        for t, _a, lab in vz.successors(blk['instrs']):
            if t in blocks and lab != 'bom':    # crashes don't flow
                succ[b].append(t)
    pred = {b: [] for b in blocks}
    for b, ts in succ.items():
        for t in ts:
            pred[t].append(b)

    seen = set()
    q = deque(['0w0'])
    while q:
        b = q.popleft()
        if b in seen:
            continue
        seen.add(b)
        q.extend(succ[b])

    # reverse postorder + iterative dominators (Cooper/Harvey/Kennedy)
    order = []
    stack = [('0w0', iter(succ['0w0']))]
    vis = {'0w0'}
    while stack:
        b, it = stack[-1]
        advanced = False
        for t in it:
            if t not in vis and t in seen:
                vis.add(t)
                stack.append((t, iter(succ[t])))
                advanced = True
                break
        if not advanced:
            order.append(b)
            stack.pop()
    rpo = list(reversed(order))
    idx = {b: i for i, b in enumerate(rpo)}
    idom = {'0w0': '0w0'}

    def meet(a, b):
        while a != b:
            while idx[a] > idx[b]:
                a = idom[a]
            while idx[b] > idx[a]:
                b = idom[b]
        return a

    changed = True
    while changed:
        changed = False
        for b in rpo[1:]:
            ps = [p for p in pred[b] if p in idom]
            if not ps:
                continue
            new = ps[0]
            for p in ps[1:]:
                new = meet(new, p)
            if idom.get(b) != new:
                idom[b] = new
                changed = True

    # definition sites: reg -> [(block, instr index)]; params/args at -1
    defs = {}
    for i in range(f['arity']):
        defs.setdefault(reg_name(i), []).append(('0w0', -1))
    for b, blk in blocks.items():
        for p in blk['params']:
            if isinstance(p, str) and REG.match(p):
                defs.setdefault(p, []).append((b, -1))
        for k, ins in enumerate(blk['instrs']):
            d = ins['fields'].get('d')
            if isinstance(d, str) and REG.match(d):
                defs.setdefault(d, []).append((b, k))

    def dominates(a, b):
        while True:
            if b == a:
                return True
            if b == '0w0':
                return False
            b = idom[b]

    violations = []
    for b in seen:
        for k, ins in enumerate(blocks[b]['instrs']):
            for key in ins['order']:
                if key == 'd':
                    continue
                for a in atoms(ins['fields'][key]):
                    if not (isinstance(a, str) and REG.match(a)):
                        continue
                    ok = False
                    for db, dk in defs.get(a, []):
                        if db == b:
                            ok = dk < k
                        elif db in idom and dominates(db, b):
                            ok = True
                        if ok:
                            break
                    if not ok:
                        kind = 'undefined' if a not in defs else 'not-dominated'
                        violations.append((b, k, ins.get('op'), key, a, kind))
    multi = sorted(r for r, ds in defs.items()
                   if len([d for d in ds if d != ('0w0', -1)]) > 1)
    return violations, multi, len(seen)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    funcs = vz.parse_ir(open(sys.argv[1], encoding='utf-8').read())
    only = set(sys.argv[2:])
    bad = 0
    for f in funcs:
        if only and f['tag'] not in only:
            continue
        violations, multi, nreach = check_function(f)
        if not violations and not multi:
            continue
        bad += 1
        print(f"{f['tag']} ({len(f['blocks'])} blocks, {nreach} reachable):")
        if multi:
            print(f"  NOT SSA -- multiple defs: {' '.join(multi)}")
        by_reg = {}
        for v in violations:
            by_reg.setdefault(v[4], []).append(v)
        for reg in sorted(by_reg):
            vs = sorted(by_reg[reg])
            where = ' '.join(f'{b}#{k}' for b, k, _o, _f, _r, _k in vs[:8])
            more = f' (+{len(vs) - 8} more)' if len(vs) > 8 else ''
            print(f"  {reg}: {len(vs)} bad use(s) [{vs[0][5]}] at {where}{more}")
    if bad == 0:
        print('clean: every use dominated by its definition, all regs single-def')
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
