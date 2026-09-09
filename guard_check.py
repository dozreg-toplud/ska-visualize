"""For each dominance-violating (or never-defined) register use, search for a
*branch-consistent* def-free path from entry: a CFG path that never takes both
sides of a branch on the same (single-def) condition registers.
No such path => the use is guarded (benign at runtime).
A found path => genuine undefined-register-use witness."""
import sys
from collections import deque
sys.path.insert(0, '/home/kirill/work/ska-visualize')
import visualize_ir as vz
from check_ssa import check_function, REG, reg_name

STATE_CAP = 20_000_000

def branch_key(term):
    op = term.get('op')
    if op in ('brn', 'brz'): return (op, term['fields']['s'])
    if op == 'clq': return ('clq', term['fields']['s'])
    if op == 'eqq': return ('eqq', term['fields']['l'], term['fields']['r'])
    return None

def analyze(f, flagged):
    blocks = f['blocks']
    # def counts (for the single-def condition filter)
    ndef = {}
    for i in range(f['arity']):
        ndef[reg_name(i)] = 1
    for b, blk in blocks.items():
        for p in blk['params']:
            if isinstance(p, str) and REG.match(p): ndef[p] = ndef.get(p, 0) + 1
        for ins in blk['instrs']:
            d = ins['fields'].get('d')
            if isinstance(d, str) and REG.match(d): ndef[d] = ndef.get(d, 0) + 1
    # branch keys: track only those in >=2 blocks with all-single-def operands
    key_of = {}
    key_blocks = {}
    for b, blk in blocks.items():
        if not blk['instrs']: continue
        k = branch_key(blk['instrs'][-1])
        if k and all(ndef.get(r, 0) == 1 for r in k[1:]):
            key_of[b] = k
            key_blocks.setdefault(k, []).append(b)
    tracked = {k: i for i, k in enumerate(sorted(k for k, bs in key_blocks.items()
                                                 if len(bs) >= 2))}
    # edges (real flow only)
    out = {}
    for b, blk in blocks.items():
        es = []
        for t, _a, lab in vz.successors(blk['instrs']):
            if t in blocks and lab != 'bom':
                ki = tracked.get(key_of.get(b)) if lab in ('z', 'o') else None
                es.append((t, ki, 0 if lab == 'z' else 1))
        out[b] = es
    # future-tracked-keys per block (reverse topo over the DAG), as bitmasks
    indeg = {b: 0 for b in blocks}
    for b, es in out.items():
        for t, _k, _s in es: indeg[t] += 1
    topo = []
    q = deque(b for b in blocks if indeg[b] == 0)
    while q:
        b = q.popleft(); topo.append(b)
        for t, _k, _s in out[b]:
            indeg[t] -= 1
            if indeg[t] == 0: q.append(t)
    future = {b: 0 for b in blocks}
    for b in reversed(topo):
        m = 0
        ki = tracked.get(key_of.get(b))
        if ki is not None: m |= (1 << ki)
        for t, _k, _s in out[b]: m |= future[t]
        future[b] = m
    results = {}
    for reg in sorted({r for _b, r in flagged}):
        has_def = {b: (reg in blk['params'] or
                       any(i['fields'].get('d') == reg for i in blk['instrs']))
                   for b, blk in blocks.items()}
        targets = {b for b, r in flagged if r == reg}
        # state: (block, frozenset of ki*2+side)
        start = ('0w0', frozenset())
        seen = {start}
        q = deque([start])
        witnesses = set()
        nstates = 0
        capped = False
        while q and len(witnesses) < len(targets):
            b, asg = q.popleft()
            nstates += 1
            if nstates > STATE_CAP:
                capped = True; break
            if b in targets: witnesses.add(b)
            if has_def[b]: continue
            for t, ki, side in out[b]:
                nasg = asg
                if ki is not None:
                    if (ki * 2 + (1 - side)) in asg: continue   # inconsistent
                    nasg = asg | {ki * 2 + side}
                fut = future[t]
                nasg = frozenset(v for v in nasg if fut >> (v // 2) & 1)
                nst = (t, nasg)
                if nst not in seen:
                    seen.add(nst); q.append(nst)
        results[reg] = (targets, witnesses, capped, nstates)
    return results

def run(path, name):
    funcs = vz.parse_ir(open(path, encoding='utf-8').read())
    print(f'== {name}')
    any_flag = False
    for f in funcs:
        viols, _multi, _n, _shape = check_function(f)
        flagged = {(b, r) for b, _k, _op, _fld, r, _kind in viols}
        if not flagged: continue
        any_flag = True
        never = sorted({r for _b, _k, _op, _fld, r, kind in viols if kind == 'undefined'})
        res = analyze(f, flagged)
        for reg, (targets, wits, capped, ns) in sorted(res.items()):
            tagx = ' (never defined!)' if reg in never else ''
            if capped:
                print(f"  {f['tag']} {reg}{tagx}: INCONCLUSIVE, cap after {ns} states")
            elif wits:
                print(f"  {f['tag']} {reg}{tagx}: GENUINE undefined use, "
                      f"{len(wits)}/{len(targets)} use blocks reachable "
                      f"on consistent def-free paths: {sorted(wits)[:6]}")
            else:
                print(f"  {f['tag']} {reg}{tagx}: all {len(targets)} uses GUARDED")
    if not any_flag: print('  (no flagged uses at all)')

if __name__ == '__main__':
    run(sys.argv[1], sys.argv[1].rsplit('/', 1)[-1])
