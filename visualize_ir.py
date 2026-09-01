#!/usr/bin/env python3
"""Visualize pretty-printed compiler IR as a self-contained HTML page.

Usage:
    python3 visualize_ir.py input.ir [-o output.html]

Reads the pretty-printer output (functions with base64-labeled basic blocks),
merges hop-chains (blocks that are each other's only successor/predecessor),
lays every function out as a layered DAG, and writes one static HTML file with
inline SVG.  No network access, no JS dependencies -- open via file://.
"""

import argparse
import html
import re
import sys
from collections import deque

# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_value(s, i):
    """Parse one value starting at s[i]; return (value, next_index).

    Values:  atoms (0v1, 0w3, 0x3d85.95cb, 0, %mov),
             lists  ~[v v ...]  or bare ~ (empty),
             structs [k=v k=v ...] / instructions [%op k=v ...]
    """
    while i < len(s) and s[i] == ' ':
        i += 1
    if i >= len(s):
        raise ValueError('unexpected end of input')
    c = s[i]
    if c == '~':
        if i + 1 < len(s) and s[i + 1] == '[':
            items = []
            i += 2
            while True:
                while i < len(s) and s[i] == ' ':
                    i += 1
                if i >= len(s):
                    raise ValueError('unterminated list')
                if s[i] == ']':
                    return items, i + 1
                v, i = parse_value(s, i)
                items.append(v)
        return [], i + 1
    if c == '[':
        i += 1
        fields = {}
        op = None
        order = []
        pos = []
        while True:
            while i < len(s) and s[i] == ' ':
                i += 1
            if i >= len(s):
                raise ValueError('unterminated struct')
            if s[i] == ']':
                d = {'op': op, 'fields': fields, 'order': order, 'pos': pos}
                return d, i + 1
            if s[i] == '%':
                j = i + 1
                while j < len(s) and s[j] not in ' ]':
                    j += 1
                op = s[i + 1:j]
                i = j
                continue
            # key=value, or a bare positional value (the ~ in [%bom ~],
            # the cells of a unit like [~ 0w3])
            j = i
            while j < len(s) and s[j] not in ' =]~[{':
                j += 1
            if j < len(s) and s[j] == '=':
                key = s[i:j]
                v, i = parse_value(s, j + 1)
                fields[key] = v
                order.append(key)
            else:
                v, i = parse_value(s, i)
                pos.append(v)
    if c == '{':
        # opaque printed noun (possibly truncated), e.g. {[[1 1.717....}
        # -- consume as a brace-balanced atom
        depth = 0
        j = i
        while j < len(s):
            if s[j] == '{':
                depth += 1
            elif s[j] == '}':
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        return s[i:j], j
    # plain atom
    j = i
    while j < len(s) and s[j] not in ' ]':
        j += 1
    if j == i:
        raise ValueError(f'unexpected {s[i]!r} at {i}: ...{s[max(0, i - 30):i + 10]!r}')
    return s[i:j], j


def parse_block_body(body):
    """Parse '{[...] [...] ...}' interior into a list of instruction dicts."""
    instrs = []
    i = 0
    while i < len(body):
        if body[i] == ' ':
            i += 1
            continue
        v, j = parse_value(body, i)
        if j <= i:
            raise ValueError(f'cannot parse block body at ...{body[i:i + 30]!r}')
        instrs.append(v)
        i = j
    return instrs


def _body_complete(s):
    """True once s contains a full brace-balanced {...} body."""
    depth = 0
    seen = False
    for ch in s:
        if ch == '{':
            depth += 1
            seen = True
        elif ch == '}':
            depth -= 1
        if seen and depth == 0:
            return True
    return False


def parse_ir(text):
    """Return a list of functions:
    {tag, arg_locs, arity, blocks: {id: {'instrs': [...], 'params': [...]}}}

    A block may span lines: `0wj:   params: ~[0vj]` with the `{...}` body on
    the following line(s); bodies may also contain brace-atoms ({[[1 1.7...}).
    """
    funcs = []
    cur = None
    pending = []       # header lines (arg locations, arity) still expected
    block_id = None    # block whose body is still being accumulated
    block_params = []
    buf = ''

    def finalize():
        nonlocal block_id, block_params, buf
        i = buf.index('{')
        depth = 0
        j = i
        while j < len(buf):
            if buf[j] == '{':
                depth += 1
            elif buf[j] == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        cur['blocks'][block_id] = {'instrs': parse_block_body(buf[i + 1:j]),
                                   'params': block_params}
        block_id, block_params, buf = None, [], ''

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip():
            continue
        if block_id is not None:           # body continuation (any indent)
            buf += ' ' + line.strip()
            if _body_complete(buf):
                finalize()
            continue
        if not line[0].isspace():
            if not line.endswith(':'):
                raise ValueError(f'line {lineno}: expected function header, got {line!r}')
            cur = {'tag': line[:-1], 'arg_locs': None, 'arity': None, 'blocks': {}}
            funcs.append(cur)
            pending = ['arg_locs', 'arity']
            continue
        if cur is None:
            raise ValueError(f'line {lineno}: content before any function header')
        s = line.strip()
        if pending:
            slot = pending.pop(0)
            if slot == 'arg_locs':
                v, _ = parse_value(s, 0)
                cur['arg_locs'] = v if isinstance(v, list) else [v]
            else:
                cur['arity'] = int(s)
            continue
        if ':' not in s:
            raise ValueError(f'line {lineno}: expected block, got {s!r}')
        bid, rest = s.split(':', 1)
        block_id = bid.strip()
        block_params = []
        rest = rest.strip()
        if rest.startswith('params:'):
            rest = rest[len('params:'):].strip()
            brace = rest.find('{')
            ptext = rest if brace < 0 else rest[:brace].strip()
            if ptext:
                v, _ = parse_value(ptext, 0)
                block_params = v if isinstance(v, list) else [v]
            rest = '' if brace < 0 else rest[brace:]
        buf = rest
        if _body_complete(buf):
            finalize()
    if block_id is not None:
        raise ValueError(f'unterminated body for block {block_id}')
    return funcs


# --------------------------------------------------------------------------
# CFG construction and hop-chain merging
# --------------------------------------------------------------------------

BRANCH_OPS = {'clq', 'eqq', 'brn'}
EXIT_OPS = {'jmp', 'jmf', 'jsp', 'jsf', 'don', 'bom'}


def jmp_target(v):
    """A 'jmp' struct in the type sense: [args=... there=0wX]."""
    return v['fields']['there'], v['fields'].get('args') or []


def unit_jmp_target(v):
    """A (unit jmp): ~ for none, or [~ 0wX] / [~ [args=... there=0wX]].
    Returns (target, args) or None."""
    if not isinstance(v, dict):
        return None
    if 'there' in v['fields']:
        return jmp_target(v)
    for p in v.get('pos', []):
        if isinstance(p, dict) and 'there' in p['fields']:
            return jmp_target(p)
        if isinstance(p, str) and p.startswith('0w'):
            return p, []
    return None


def successors(instrs):
    """Return [(target_block, args, label)] for the block's terminator."""
    if not instrs:
        return []
    term = instrs[-1]
    op = term.get('op')
    if op == 'hop':
        t, a = jmp_target(term['fields']['t'])
        return [(t, a, '')]
    if op in BRANCH_OPS:
        z, za = jmp_target(term['fields']['z'])
        o, oa = jmp_target(term['fields']['o'])
        return [(z, za, 'z'), (o, oa, 'o')]
    if op == 'bom':
        # crash, but may name the block control would otherwise reach
        t = unit_jmp_target(term['fields'].get('o'))
        if t:
            return [(t[0], t[1], 'bom')]
    return []


def build_nodes(func):
    """Merge hop-chains; return (nodes, edges).

    nodes: {node_id: {'ids': [block ids in order], 'instrs': [...]}}
    edges: [(src_node, dst_node, args, label)]
    """
    blocks = func['blocks']
    pred_count = {b: 0 for b in blocks}
    for b, bl in blocks.items():
        for t, _a, _l in successors(bl['instrs']):
            if t in pred_count:
                pred_count[t] += 1

    def merge_next(b):
        """Target b's empty-args %hop chain-merges into, or None."""
        ins = blocks[b]['instrs']
        if not ins or ins[-1].get('op') != 'hop':
            return None
        t, args = jmp_target(ins[-1]['fields']['t'])
        if (not args and t != b and t in blocks
                and pred_count[t] == 1 and not blocks[t]['params']):
            return t
        return None

    # chain heads are blocks nothing merges into; walk each chain once
    merge_targets = {t for b in blocks if (t := merge_next(b))}
    nodes = {}
    assigned = set()
    for b in blocks:
        if b in merge_targets:
            continue
        ids = [b]
        instrs = []
        cur = b
        t = merge_next(cur)
        while t:
            instrs.extend(blocks[cur]['instrs'][:-1])   # drop the hop
            ids.append(t)
            cur = t
            t = merge_next(cur)
        instrs.extend(blocks[cur]['instrs'])
        nodes[b] = {'ids': ids, 'instrs': instrs, 'params': list(blocks[b]['params'])}
        assigned.update(ids)
    for b in blocks:            # safety net for hop cycles: emit unmerged
        if b not in assigned:
            nodes[b] = {'ids': [b], 'instrs': list(blocks[b]['instrs']),
                        'params': list(blocks[b]['params'])}

    edges = []
    for b, n in nodes.items():
        for t, args, label in successors(n['instrs']):
            if t in nodes:
                edges.append((b, t, args, label))
    return nodes, edges


# --------------------------------------------------------------------------
# Text rendering of values / instructions
# --------------------------------------------------------------------------

NUM_RE = re.compile(r'^\d+(?:\.\d{3})*$')
HINT_OPS = {'hse', 'hsp', 'hdp'}


def try_cord(numstr):
    """Decode a dotted decimal as little-endian printable ASCII, or None."""
    n = int(numstr.replace('.', ''))
    if n < 32:
        return None
    try:
        t = n.to_bytes((n.bit_length() + 7) // 8, 'little').decode('ascii')
    except UnicodeDecodeError:
        return None
    if all(32 <= ord(c) < 127 for c in t):
        return t
    return None


def spot_regs(func):
    """Registers whose value ends up in a %spot hint -- immediates written to
    them are source locations and get the spot rendering."""
    regs = set()
    for bl in func['blocks'].values():
        for ins in bl['instrs']:
            if ins.get('op') in HINT_OPS and ins['fields'].get('n') == '%spot':
                p = ins['fields'].get('p')
                if isinstance(p, str):
                    regs.add(p)
    return regs


def _sub_cords_in_opaque(s):
    """Replace decodable dotted numbers inside a {...} brace atom, skipping
    any number that runs into the ....-truncation marker."""
    def repl(m):
        if s[m.end():m.end() + 1] == '.':     # truncated digits -- don't guess
            return m.group(0)
        t = try_cord(m.group(0))
        return f"'{t}'" if t else m.group(0)
    return re.sub(r'\d+(?:\.\d{3})*', repl, s)


def fmt_path(v):
    """A null-terminated list of cords -> /gen/test-compile/hoon, or None."""
    if not isinstance(v, dict) or v.get('op') or v['order']:
        return None
    elems = v.get('pos', [])
    if len(elems) < 2 or elems[-1] not in ('0', []):
        return None
    parts = []
    for e in elems[:-1]:
        if not isinstance(e, str) or not NUM_RE.match(e):
            return None
        t = try_cord(e)
        if t is None:
            return None
        parts.append(t)
    return '/' + '/'.join(parts)


def fmt_spot(v):
    """Render a %spot immediate: [/gen/test-compile/hoon [13 22] 13 25].
    Falls back to cord-or-number rendering if the shape doesn't match."""
    if isinstance(v, dict) and not v.get('op') and not v['order'] and v.get('pos'):
        path = fmt_path(v['pos'][0])
        if path:
            rest = ' '.join(fmt_value(x) for x in v['pos'][1:])
            return f'[{path} {rest}]' if rest else f'[{path}]'
    return fmt_value(v, cords=True)


def fmt_value(v, cords=False):
    if isinstance(v, list):
        return '~' if not v else '~[' + ' '.join(fmt_value(x, cords) for x in v) + ']'
    if isinstance(v, dict):
        inner = []
        if v.get('op'):
            inner.append('%' + v['op'])
        inner += [fmt_value(p, cords) for p in v.get('pos', [])]
        for k in v['order']:
            inner.append(f'{k}={fmt_value(v["fields"][k], cords)}')
        return '[' + ' '.join(inner) + ']'
    if cords:
        if v.startswith('{'):
            return _sub_cords_in_opaque(v)
        if NUM_RE.match(v):
            t = try_cord(v)
            return f"'{t}'" if t else v
    return v


def instr_parts(ins, spots=frozenset()):
    """Return (opcode, operand string) for a non-branch instruction."""
    op = ins.get('op') or '?'
    parts = []
    for k in ins['order']:
        v = ins['fields'][k]
        if op == 'imm' and k == 'n' and ins['fields'].get('d') in spots:
            parts.append(f'{k}={fmt_spot(v)}')
        else:
            parts.append(f'{k}={fmt_value(v)}')
    return '%' + op, ' '.join(parts)


def branch_cond_parts(ins):
    """Branch terminator shown without its z/o targets (edges carry those)."""
    op = ins.get('op')
    keep = [k for k in ins['order'] if k not in ('z', 'o')]
    ops = ' '.join(f'{k}={fmt_value(ins["fields"][k])}' for k in keep)
    return '%' + op, ops


# --------------------------------------------------------------------------
# Layout (layered DAG: longest-path layering + barycenter ordering)
# --------------------------------------------------------------------------

FONT = 12          # px, monospace
CHAR_W = 7.3       # advance estimate for 12px ui-monospace
HDR_CW = 6.5       # advance estimate for the 11px header font
LINE_H = 17
HDR_H = 20
HDR_LH = 15        # extra header (wrapped id enumeration) lines
ENTRY_W = 46       # room reserved for the "entry" tag on the first header line
PAD_X = 12
PAD_Y = 8
NODE_GAP_X = 36
LAYER_GAP = 64
MARGIN = 28


def node_geometry(node, entry, spots=frozenset()):
    ids = node['ids']
    is_entry = ids[0] == entry
    lines = []          # (opcode, operands, kind)
    if node['params']:
        lines.append(('params', fmt_value(node['params']), 'params'))
    instrs = node['instrs']
    for k, ins in enumerate(instrs):
        is_term = (k == len(instrs) - 1)
        op = ins.get('op')
        if is_term and op == 'hop':
            continue    # the outgoing edge says it all
        if is_term and op in BRANCH_OPS:
            oc, rest = branch_cond_parts(ins)
            lines.append((oc, rest, 'branch'))
        elif is_term and op in EXIT_OPS:
            if op == 'bom' and unit_jmp_target(ins['fields'].get('o')):
                oc, rest = branch_cond_parts(ins)   # edge carries the target
            else:
                oc, rest = instr_parts(ins, spots)
            lines.append((oc, rest, 'exit'))
        else:
            oc, rest = instr_parts(ins, spots)
            lines.append((oc, rest, 'plain'))
    code_w = max([(len(oc) + 1 + len(rest)) * CHAR_W for oc, rest, _ in lines],
                 default=0)
    # wrap the id enumeration so it never widens the box past the code;
    # floor keeps code-less nodes from wrapping one id per line
    target_w = max(code_w, 150,
                   max(len(i) for i in ids) * HDR_CW + (ENTRY_W if is_entry else 0))
    header_lines = []
    cur = ''
    for i in ids:
        cand = i if not cur else cur + ' · ' + i
        budget = target_w - (ENTRY_W if is_entry and not header_lines else 0)
        if cur and len(cand) * HDR_CW > budget:
            header_lines.append(cur)
            cur = i
        else:
            cur = cand
    header_lines.append(cur)
    hdr_w = max(len(hl) * HDR_CW for hl in header_lines)
    if is_entry:
        hdr_w = max(hdr_w, len(header_lines[0]) * HDR_CW + ENTRY_W)
    w = max(84, max(code_w, hdr_w) + 2 * PAD_X)
    h = (HDR_H + HDR_LH * (len(header_lines) - 1)
         + len(lines) * LINE_H + 2 * PAD_Y)
    return header_lines, lines, w, h


def layer_nodes(nodes, edges, entry):
    """Layer the graph and expand multi-layer edges into dummy waypoints.

    Returns (layers, gsucc, gpred, chains, layer) where layers/gsucc/gpred
    include one dummy vertex ('d', edge_index, k) per intermediate layer an
    edge crosses, and chains maps edge index -> its dummy vertices in order.
    Dummies take part in ordering and x-placement, so long edges get routed
    through the gaps between boxes instead of across them.
    """
    succs = {b: [] for b in nodes}
    preds = {b: [] for b in nodes}
    for s, d, _a, _l in edges:
        succs[s].append(d)
        preds[d].append(s)
    # longest path from roots (Kahn topological order)
    indeg = {b: len(preds[b]) for b in nodes}
    layer = {b: 0 for b in nodes}
    topo = []
    q = deque(b for b in nodes if indeg[b] == 0)
    while q:
        b = q.popleft()
        topo.append(b)
        for t in succs[b]:
            layer[t] = max(layer[t], layer[b] + 1)
            indeg[t] -= 1
            if indeg[t] == 0:
                q.append(t)
    if len(topo) != len(nodes):        # cycle safety net: dump leftovers below
        deepest = max(layer.values(), default=0)
        for b in nodes:
            if b not in topo:
                deepest += 1
                layer[b] = deepest

    # split edges spanning >1 layer with dummy vertices
    gsucc = {b: [] for b in nodes}
    gpred = {b: [] for b in nodes}
    chains = {}
    for ei, (s, d, _a, _l) in enumerate(edges):
        chain = []
        prev = s
        for k in range(layer[s] + 1, layer[d]):
            dm = ('d', ei, k)
            layer[dm] = k
            gsucc[dm] = []
            gpred[dm] = []
            gsucc[prev].append(dm)
            gpred[dm].append(prev)
            chain.append(dm)
            prev = dm
        gsucc[prev].append(d)
        gpred[d].append(prev)
        chains[ei] = chain

    nlayers = max(layer.values(), default=0) + 1
    layers = [[] for _ in range(nlayers)]
    # deterministic initial order: iterative DFS from entry (chains can be
    # thousands of blocks deep -- no recursion)
    seen = set()
    order = []

    def dfs(root):
        stack = [root]
        while stack:
            b = stack.pop()
            if b in seen:
                continue
            seen.add(b)
            order.append(b)
            stack.extend(reversed(gsucc[b]))

    if entry in nodes:
        dfs(entry)
    for b in sorted(nodes):
        dfs(b)
    for b in order:
        layers[layer[b]].append(b)

    # barycenter sweeps
    pos = {}
    for row in layers:
        for i, b in enumerate(row):
            pos[b] = i
    for sweep in range(6):
        rng = range(1, nlayers) if sweep % 2 == 0 else range(nlayers - 2, -1, -1)
        neigh = gpred if sweep % 2 == 0 else gsucc
        for li in rng:
            row = layers[li]
            keyed = []
            for b in row:
                ns = [pos[n] for n in neigh[b] if n in pos]
                keyed.append((sum(ns) / len(ns) if ns else pos[b], pos[b], str(b)))
            keyed.sort()
            byname = {str(b): b for b in row}
            layers[li] = [byname[nm] for _k, _p, nm in keyed]
            for i, b in enumerate(layers[li]):
                pos[b] = i
    return layers, gsucc, gpred, chains, layer


def assign_coords(layers, succs, preds, sizes):
    x = {}
    y = {}
    rowtop = []
    rowh = []
    ytop = MARGIN
    for row in layers:
        rh = max((sizes[b][1] for b in row), default=0)
        rowtop.append(ytop)
        rowh.append(rh)
        for b in row:
            y[b] = ytop
        cx = MARGIN
        for b in row:
            w = sizes[b][0]
            x[b] = cx + w / 2
            cx += w + NODE_GAP_X
        ytop += rh + LAYER_GAP

    def settle(row, neigh):
        desired = []
        for b in row:
            ns = [x[n] for n in neigh[b] if n in x]
            desired.append(sum(ns) / len(ns) if ns else x[b])
        # keep order, enforce min gaps around desired positions
        left = -1e18
        placed = []
        for b, d in zip(row, desired):
            w = sizes[b][0]
            c = max(d, left + NODE_GAP_X + w / 2)
            placed.append(c)
            left = c + w / 2
        # pull the whole row back toward its desired mean
        shift = sum(p - d for p, d in zip(placed, desired)) / len(placed)
        for b, p in zip(row, placed):
            x[b] = p - shift

    for it in range(8):
        rng = range(1, len(layers)) if it % 2 == 0 else range(len(layers) - 2, -1, -1)
        neigh = preds if it % 2 == 0 else succs
        for li in rng:
            if layers[li]:
                settle(layers[li], neigh)
    minx = min((x[b] - sizes[b][0] / 2 for row in layers for b in row), default=0)
    for b in x:
        x[b] += MARGIN - minx
    width = max((x[b] + sizes[b][0] / 2 for row in layers for b in row), default=0) + MARGIN
    height = ytop - LAYER_GAP + MARGIN
    return x, y, rowtop, rowh, width, height


# --------------------------------------------------------------------------
# SVG generation
# --------------------------------------------------------------------------

def esc(s):
    return html.escape(str(s), quote=True)


def render_function(func, known_tags):
    entry = '0w0'
    nodes, edges = build_nodes(func)
    spots = spot_regs(func)
    geo = {b: node_geometry(n, entry, spots) for b, n in nodes.items()}
    sizes = {b: (g[2], g[3]) for b, g in geo.items()}
    layers, gsucc, gpred, chains, layer = layer_nodes(nodes, edges, entry)
    for chain in chains.values():
        for dm in chain:
            sizes[dm] = (10.0, 0.0)
    x, y, rowtop, rowh, width, height = assign_coords(layers, gsucc, gpred, sizes)

    svg = []
    svg.append(f'<svg viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" '
               f'height="{height:.0f}" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace">')
    svg.append('''<defs>
  <marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0 0 L10 5 L0 10 z" fill="var(--edge)"/></marker>
  <marker id="arr-z" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0 0 L10 5 L0 10 z" fill="var(--edge-z)"/></marker>
  <marker id="arr-o" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0 0 L10 5 L0 10 z" fill="var(--edge-o)"/></marker>
</defs>''')

    # spread multi-edge attachment points along node borders; sort by the
    # first/last routed waypoint so fan-outs don't cross right at the border
    out_at = {}
    in_at = {}
    by_src = {}
    by_dst = {}
    for ei, e in enumerate(edges):
        by_src.setdefault(e[0], []).append(ei)
        by_dst.setdefault(e[1], []).append(ei)

    def first_wp_x(ei):
        c = chains[ei]
        return x[c[0]] if c else x[edges[ei][1]]

    def last_wp_x(ei):
        c = chains[ei]
        return x[c[-1]] if c else x[edges[ei][0]]

    for b, eis in by_src.items():
        eis.sort(key=first_wp_x)
        w = sizes[b][0]
        for i, ei in enumerate(eis):
            out_at[ei] = x[b] - w * 0.35 + w * 0.7 * (i + 1) / (len(eis) + 1)
    for b, eis in by_dst.items():
        eis.sort(key=last_wp_x)
        w = sizes[b][0]
        for i, ei in enumerate(eis):
            in_at[ei] = x[b] - w * 0.35 + w * 0.7 * (i + 1) / (len(eis) + 1)

    def seg(p, q):
        """Cubic from p to q with vertical tangents at both ends."""
        dy = max(18.0, min(52.0, (q[1] - p[1]) * 0.5))
        return (f'C {p[0]:.1f} {p[1] + dy:.1f}, {q[0]:.1f} {q[1] - dy:.1f}, '
                f'{q[0]:.1f} {q[1]:.1f}')

    # edges under nodes; labels collected and drawn on top of everything
    labels = []
    label_rank = {}     # (dst, n-th labeled edge into dst) for chip stacking
    for ei, (s, d, args, label) in enumerate(edges):
        p = (out_at[ei], y[s] + sizes[s][1])
        parts = [f'M{p[0]:.1f} {p[1]:.1f}']
        for dm in chains[ei]:
            li = layer[dm]
            top = (x[dm], rowtop[li])
            bot = (x[dm], rowtop[li] + rowh[li])
            parts.append(seg(p, top))
            if bot[1] > top[1]:
                parts.append(f'L {bot[0]:.1f} {bot[1]:.1f}')
            p = bot
        end = (in_at[ei], y[d] - 1)
        parts.append(seg(p, end))
        cls = {'z': 'edge-z', 'o': 'edge-o', 'bom': 'edge-bom'}.get(label, 'edge-plain')
        marker = {'z': 'arr-z', 'o': 'arr-o'}.get(label, 'arr')
        svg.append(f'<g class="edge {cls}"><path d="{" ".join(parts)}" fill="none" '
                   f'marker-end="url(#{marker})"/></g>')
        text = '' if label == 'bom' else label   # the dash pattern says "crash path"
        if args:
            text = (label + ' ' if label else '') + fmt_value(args)
        if text:
            rank = label_rank.get(d, 0)
            label_rank[d] = rank + 1
            # in the clear inter-layer gap just above the arrowhead; stack
            # chips of same-target edges so they never sit on one another
            ly = y[d] - 20 - 22 * rank
            tw = len(text) * (CHAR_W - 1) + 10
            labels.append((cls, end[0], ly, tw, text))

    # nodes
    for b in nodes:
        header_lines, lines, w, h = geo[b]
        nx = x[b] - w / 2
        ny = y[b]
        is_entry = nodes[b]['ids'][0] == entry
        svg.append(f'<g class="node{" node-entry" if is_entry else ""}">')
        svg.append(f'<rect x="{nx:.1f}" y="{ny:.1f}" width="{w:.1f}" height="{h:.1f}" rx="8"/>')
        hy = ny + PAD_Y + 9
        for hl in header_lines:
            svg.append(f'<text class="hdr" x="{nx + PAD_X:.1f}" y="{hy:.1f}" '
                       f'font-size="11">{esc(hl)}</text>')
            hy += HDR_LH
        if is_entry:
            svg.append(f'<text class="entry-tag" x="{nx + w - PAD_X:.1f}" y="{ny + PAD_Y + 9:.1f}" '
                       f'text-anchor="end" font-size="10">entry</text>')
        ty = hy - HDR_LH + 23   # first code baseline, 23px under last header line
        for oc, rest, kind in lines:
            cls = {'branch': 'op-branch', 'exit': 'op-exit', 'params': 'op-params'}.get(kind, 'op')
            body = esc(rest)
            if rest:
                # link call/tail-call targets to their function section
                for tag in known_tags:
                    etag = esc(tag)
                    if etag in body:
                        body = body.replace(
                            etag, f'<a href="#fn-{anchor(tag)}" class="fnlink">{etag}</a>')
            svg.append(f'<text class="{cls}" x="{nx + PAD_X:.1f}" y="{ty:.1f}" font-size="{FONT}">'
                       f'<tspan class="opcode">{esc(oc)}</tspan> {body}</text>')
            ty += LINE_H
        svg.append('</g>')

    # edge labels last, above the boxes
    for cls, lx, ly, tw, text in labels:
        svg.append(f'<g class="edge {cls}">'
                   f'<rect class="edge-chip" x="{lx - tw / 2:.1f}" y="{ly - 9:.1f}" '
                   f'width="{tw:.1f}" height="18" rx="4"/>'
                   f'<text class="edge-label" x="{lx:.1f}" y="{ly + 4:.1f}" '
                   f'text-anchor="middle" font-size="11">{esc(text)}</text></g>')
    svg.append('</svg>')
    return '\n'.join(svg)


def anchor(tag):
    return tag.replace('0x', '').replace('.', '-')


# --------------------------------------------------------------------------
# HTML page
# --------------------------------------------------------------------------

CSS = '''
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --muted: #898781; --border: rgba(11,11,11,0.14);
  --edge: #898781; --edge-z: #2a78d6; --edge-o: #eb6834;
  --entry: #2a78d6; --chip: #f0efec;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
    --muted: #898781; --border: rgba(255,255,255,0.16);
    --edge: #898781; --edge-z: #3987e5; --edge-o: #d95926;
    --entry: #3987e5; --chip: #262624;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
  --muted: #898781; --border: rgba(255,255,255,0.16);
  --edge: #898781; --edge-z: #3987e5; --edge-o: #d95926;
  --entry: #3987e5; --chip: #262624;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: none; padding: 24px 32px 64px; }
h1 { font-size: 18px; font-weight: 600; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: 13px; margin: 0 0 24px; }
section { margin: 0 0 40px; }
h2 {
  font-size: 15px; font-weight: 600; margin: 0 0 2px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.meta { color: var(--ink-2); font-size: 13px; margin: 0 0 12px; }
.meta code { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
.canvas {
  overflow-x: auto; background: var(--surface);
  border: 1px solid var(--border); border-radius: 10px; padding: 8px;
}
.legend { display: flex; gap: 18px; align-items: center; margin: 10px 2px 0; color: var(--ink-2); font-size: 12px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 18px; height: 0; border-top: 2px solid; display: inline-block; }
.node rect { fill: var(--surface); stroke: var(--border); stroke-width: 1; }
.node-entry > rect { stroke: var(--entry); stroke-width: 1.5; }
.node text { fill: var(--ink); }
.node .hdr { fill: var(--muted); }
.node .entry-tag { fill: var(--entry); font-weight: 600; }
.opcode { font-weight: 600; }
.op-params { fill: var(--ink-2); font-style: italic; }
.op-params .opcode { fill: var(--muted); font-weight: 400; font-style: italic; }
.op-exit .opcode { fill: var(--edge-z); }
.op-branch .opcode { fill: var(--ink); }
.fnlink { fill: var(--edge-z); text-decoration: underline; }
.edge path { stroke-width: 1.5; }
.edge-plain path { stroke: var(--edge); }
.edge-z path { stroke: var(--edge-z); }
.edge-o path { stroke: var(--edge-o); }
.edge-bom { opacity: 0.55; }
.edge-bom path { stroke: var(--edge); stroke-dasharray: 5 4; }
.edge:hover path { stroke-width: 2.5; }
.edge-bom:hover { opacity: 1; }
.edge-chip { fill: var(--chip); stroke: var(--border); stroke-width: 0.5; }
.edge-label { fill: var(--ink-2); font-weight: 600; }
'''


def build_page(funcs, source_name):
    tags = [f['tag'] for f in funcs]
    parts = []
    parts.append('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">')
    parts.append(f'<title>IR CFG — {esc(source_name)}</title>')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append(f'<style>{CSS}</style>\n</head>\n<body>\n<main>')
    parts.append(f'<h1>IR control-flow graphs</h1>')
    parts.append(f'<p class="sub">{esc(source_name)} · {len(funcs)} function'
                 f'{"s" if len(funcs) != 1 else ""} · hop-chains merged</p>')
    for f in funcs:
        locs = fmt_value(f['arg_locs']) if f['arg_locs'] is not None else '~'
        parts.append(f'<section id="fn-{anchor(f["tag"])}">')
        parts.append(f'<h2>{esc(f["tag"])}</h2>')
        parts.append(f'<p class="meta">{f["arity"]} arg{"s" if f["arity"] != 1 else ""} · '
                     f'argument slots <code>{esc(locs)}</code></p>')
        parts.append('<div class="canvas">')
        parts.append(render_function(f, tags))
        parts.append('</div>')
        parts.append('<div class="legend">'
                     '<span><i class="swatch" style="border-color:var(--edge-z)"></i>z (yes / zero / cell)</span>'
                     '<span><i class="swatch" style="border-color:var(--edge-o)"></i>o (no / other)</span>'
                     '<span><i class="swatch" style="border-color:var(--edge)"></i>unconditional</span>'
                     '<span><i class="swatch" style="border-color:var(--edge);border-top-style:dashed;'
                     'opacity:.55"></i>%bom fallthrough (not taken — crash)</span>'
                     '</div>')
        parts.append('</section>')
    parts.append('</main>\n</body>\n</html>')
    return '\n'.join(parts)


def main():
    ap = argparse.ArgumentParser(description='Render pretty-printed IR as an HTML CFG page.')
    ap.add_argument('input', help='pretty-printer output file')
    ap.add_argument('-o', '--output', help='output HTML file (default: <input>.html)')
    args = ap.parse_args()
    with open(args.input, encoding='utf-8') as fh:
        text = fh.read()
    funcs = parse_ir(text)
    if not funcs:
        sys.exit('no functions found in input')
    out = args.output or args.input + '.html'
    page = build_page(funcs, args.input.rsplit('/', 1)[-1])
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write(page)
    total_blocks = sum(len(f['blocks']) for f in funcs)
    print(f'{len(funcs)} function(s), {total_blocks} blocks -> {out}')


if __name__ == '__main__':
    main()
