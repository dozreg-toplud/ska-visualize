#!/usr/bin/env python3
"""
Interactive call-graph explorer (HTML+D3) with callsite fan-out.

  python3 build_explorer.py out.txt explorer.html

Click a node -> its DEDUPLICATED immediate callees.
Each callee shows a `fork K` pill when the parent calls it at K>1 callsites;
clicking it fans the callee into K instances (one per callsite, labelled with
the callsite location). Clicking an instance drills into that function's own
deduplicated callees. Root = the in-degree-0 function.
"""

import json, re, sys
from collections import defaultdict

VERTEX_RE = re.compile(r"(0x[0-9a-f]+):\s*\[sock=(\S+)\s+fol=(\S+)\]")
LOCATION_RE = re.compile(
    r"at\s+(\S+):\s*<\[([\d.]+)\s+([\d.]+)\]\.\[([\d.]+)\s+([\d.]+)\]>")
GROUP_RE = re.compile(r"\(([^)]*)\)")
HEX_RE = re.compile(r"0x[0-9a-f]+")
CALLEE_RE = re.compile(
    r"(0x[0-9a-f]+)\s+at\s+(\S+?):(?:\s*<\[([\d.]+)\s+([\d.]+)\]\.\[([\d.]+)\s+([\d.]+)\]>)?")


def num(s): return s.replace(".", "")


def parse(text):
    func_text, _, edge_text = text.partition("callgraph:")

    files, fidx = [], {}
    def fi(p):
        if p not in fidx:
            fidx[p] = len(files); files.append(p)
        return fidx[p]

    meta = {}
    for line in func_text.splitlines():
        m = VERTEX_RE.search(line)
        if not m:
            continue
        idx = m.group(1)
        loc = LOCATION_RE.search(line)
        if loc:
            path, l1, c1, l2, c2 = loc.groups()
            meta[idx] = [fi(path), f"{num(l1)}:{num(c1)}\u2013{num(l2)}:{num(c2)}"]
        else:
            meta[idx] = [fi("??"), ""]

    edges = {}
    for g in GROUP_RE.finditer(edge_text):
        inner = g.group(1)
        hexes = HEX_RE.findall(inner)
        if not hexes:
            continue
        src = hexes[0]
        sites = []
        for m in CALLEE_RE.finditer(inner):
            dst, path, l1, c1, l2, c2 = m.groups()
            pos = f"{num(l1)}:{num(c1)}\u2013{num(l2)}:{num(c2)}" if l1 else ""
            sites.append([dst, fi(path), pos])
        edges[src] = sites

    return files, meta, edges


def find_root(meta, edges):
    nodes = set(meta) | set(edges) | {s[0] for v in edges.values() for s in v}
    indeg = defaultdict(int)
    for src, sites in edges.items():
        for s in sites:
            if s[0] != src:
                indeg[s[0]] += 1
    roots = [n for n in nodes if indeg[n] == 0]
    return roots[0] if roots else next(iter(nodes))


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>call-graph explorer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<style>
  :root{
    --bg:#0c0e14; --bg2:#11141d; --card:#141826;
    --ink:#e6e9f0; --dim:#8a92a6; --faint:#5b6378;
    --root:#34d399; --branch:#38bdf8; --leaf:#5b6378; --site:#fbbf24; --rec:#c084fc;
    --edge:#2a3550; --edge-site:#7c5b1e;
    --mono:'JetBrains Mono',ui-monospace,Menlo,Consolas,monospace;
    --sans:'IBM Plex Sans',system-ui,sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;width:100%;height:100%;overflow:hidden;
    background:radial-gradient(1200px 800px at 70% -10%, #141a2b 0%, transparent 60%), var(--bg);
    color:var(--ink);font-family:var(--sans)}
  #chart{position:fixed;inset:0;width:100vw;height:100vh;display:block}
  header{position:fixed;top:0;left:0;right:0;z-index:5;display:flex;align-items:center;
    gap:16px;padding:12px 18px;pointer-events:none;
    background:linear-gradient(180deg, rgba(12,14,20,.92), rgba(12,14,20,0))}
  header .title{font-weight:600;font-size:15px}
  header .title b{color:var(--site);font-family:var(--mono);font-weight:700}
  header .stat{color:var(--dim);font-size:12.5px;font-family:var(--mono)}
  header .spacer{flex:1}
  button{pointer-events:auto;font-family:var(--mono);font-size:12px;color:var(--ink);
    background:var(--bg2);border:1px solid #232a3b;padding:6px 11px;border-radius:7px;
    cursor:pointer;transition:.15s}
  button:hover{border-color:var(--branch);color:var(--branch)}
  .panel{position:fixed;z-index:5;font-family:var(--mono);font-size:11.5px;color:var(--dim);
    background:rgba(17,20,29,.8);border:1px solid #1d2331;border-radius:9px;padding:10px 12px;
    backdrop-filter:blur(6px)}
  #legend{left:18px;bottom:18px;line-height:1.9}
  #legend .sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:7px;vertical-align:middle}
  #legend .pill{color:var(--site);border:1px solid var(--edge-site);border-radius:6px;padding:0 5px}
  #hint{right:18px;bottom:18px;max-width:250px;line-height:1.6;color:var(--faint)}
  #hint b{color:var(--dim)}
  .link{fill:none;stroke:var(--edge);stroke-width:1.3px}
  .link.tosite{stroke:var(--edge-site);stroke-dasharray:3 3}
  .node{cursor:pointer}
  .node .card{fill:var(--card);stroke:#283149;stroke-width:1.3px;transition:stroke .15s,filter .15s}
  .node:hover .card{stroke:var(--branch);filter:drop-shadow(0 0 6px rgba(56,189,248,.4))}
  .node.is-root .card{stroke:var(--root)}
  .node.is-site .card{stroke:var(--edge-site);stroke-dasharray:4 3;fill:#171420}
  .node.is-site:hover .card{stroke:var(--site);filter:drop-shadow(0 0 6px rgba(251,191,36,.35))}
  .node.is-recursive .card{stroke:var(--rec);stroke-dasharray:5 3;fill:#1a1426}
  .node.is-recursive:hover .card{stroke:var(--rec);filter:drop-shadow(0 0 8px rgba(192,132,252,.55))}
  .node.is-recursive .hex{fill:var(--rec)}
  .node.is-recursive .drill{fill:var(--rec);font-size:14px}
  .backedge{fill:none;stroke:var(--rec);stroke-width:2.5px;stroke-dasharray:6 4;
            filter:drop-shadow(0 0 7px rgba(192,132,252,.8))}
  .recring{fill:none;stroke:var(--rec);stroke-width:2px}
  .node.pulse .card{stroke:var(--rec);filter:drop-shadow(0 0 12px rgba(192,132,252,.9))}
  .node text{font-family:var(--mono);dominant-baseline:middle;pointer-events:none}
  .node .hex{fill:var(--ink);font-weight:700;font-size:12px}
  .node.is-root .hex{fill:var(--root)}
  .node.is-site .hex{fill:var(--site)}
  .node .l2{fill:var(--dim);font-size:10px}
  .node .l3{fill:var(--faint);font-size:10px}
  .node .drill{fill:var(--branch);font-size:11px;font-weight:600;text-anchor:end}
  .node.expanded .drill{fill:var(--ink)}
  .fan{cursor:pointer}
  .fan rect{fill:#241c08;stroke:var(--edge-site);stroke-width:1px}
  .fan:hover rect{fill:#3a2c0c}
  .fan text{fill:var(--site);font-size:9.5px;font-weight:600;font-family:var(--mono);
    text-anchor:middle;dominant-baseline:middle;pointer-events:none}
  .merge{cursor:pointer}
  .merge rect{fill:#241c08;stroke:var(--edge-site);stroke-width:1px}
  .merge:hover rect{fill:#3a2c0c}
  .merge text{fill:var(--site);font-size:11px;font-family:var(--mono);text-anchor:middle;dominant-baseline:middle}
  .merge:hover text{fill:#fff}
  @keyframes rootpulse{0%,100%{filter:drop-shadow(0 0 0 rgba(52,211,153,0))}
                       50%{filter:drop-shadow(0 0 11px rgba(52,211,153,.55))}}
  .node.is-root .card{animation:rootpulse 2.2s ease-in-out infinite}
</style>
</head>
<body>
<header>
  <span class="title">call-graph explorer &nbsp;<b>__ROOT__</b></span>
  <span class="stat" id="counts"></span>
  <span class="spacer"></span>
  <button id="fit">fit view</button>
  <button id="reset">reset to root</button>
</header>
<div id="info"></div>
<div id="legend" class="panel">
  <div><span class="sw" style="background:var(--root)"></span>root</div>
  <div><span class="sw" style="background:var(--branch)"></span>function &mdash; click for unique callees</div>
  <div><span class="sw" style="background:var(--site)"></span>call-site instance</div>
  <div style="margin-top:5px"><span class="pill">&#9095; K</span> &nbsp;fan into K call-sites</div>
</div>
<div id="hint" class="panel">
  click a node for its <b>unique callees</b>. a <b>&#9095;K</b> pill means the parent calls it
  at K sites &mdash; click to fan them out; <b>&#10554;</b> merges back. scroll to zoom, drag to pan.
</div>
<svg id="chart" width="100%" height="100%">
  <defs>
    <marker id="recarrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#c084fc"/>
    </marker>
  </defs>
</svg>

<script>
if (typeof d3 === 'undefined'){
  document.body.innerHTML =
    '<div style="color:#e6e9f0;font-family:monospace;padding:48px;line-height:1.6">'+
    'D3 could not load from the CDN.<br>Open with an internet connection, or ask for an offline build.</div>';
  throw new Error('d3 missing');
}
const FILES = __FILES__;
const META  = __META__;     // hex -> [fileIdx, pos]
const EDGES = __EDGES__;    // hex -> [[dst, fileIdx, pos], ...] (ordered, dups = callsites)
const ROOT  = "__ROOT__";

const fileOf = i => (i==null ? "(no source)" : FILES[i]);
function defMeta(h){ const m=META[h]; if(!m) return {f:"(no source)",p:""};
  const f=fileOf(m[0]); return {f: (f==="??"?"(no source)":f), p:m[1]}; }
function calleeCount(h){ const e=EDGES[h]; if(!e) return 0;
  const s=new Set(); for(const t of e) s.add(t[0]); return s.size; }

const BOX_W=196, BOX_H=58, H_GAP=228, V_GAP=132;

let uid=0;
function mk(hex, sites, site, parent){
  return {uid:uid++, hex, sites:sites||[], site:site||null, parent:parent||null,
          open:false, fanned:false, _dedup:null, _instances:null};
}
const treeRoot = mk(ROOT, [], null, null);

// d3 children accessor: returns visible children, expanding fanned callees into instances
function kids(n){
  if(!n.open) return null;
  if(!n._dedup){
    const e = EDGES[n.hex] || [];
    const order=[], by=new Map();
    for(const [dst,fi,pos] of e){
      if(!by.has(dst)){ by.set(dst,[]); order.push(dst); }
      by.get(dst).push({f:fileOf(fi), p:pos});
    }
    n._dedup = order.map(dst => mk(dst, by.get(dst), null, n));
  }
  const out=[];
  for(const d of n._dedup){
    if(d.fanned && d.sites.length>1){
      if(!d._instances) d._instances = d.sites.map(s => { const m=mk(d.hex,[s],s,n); m.ref=d; return m; });
      for(const ins of d._instances) out.push(ins);
    } else out.push(d);
  }
  return out.length ? out : null;
}

const svg = d3.select("#chart");
const gZoom = svg.append("g");
const gLink = gZoom.append("g");
const gNode = gZoom.append("g");
const gFlash = gZoom.append("g").attr("class","flash");
const tree = d3.tree().nodeSize([H_GAP, V_GAP]);
const zoom = d3.zoom().scaleExtent([0.12, 2.5]).on("zoom", e => gZoom.attr("transform", e.transform));
svg.call(zoom);

const pos = new Map();
let first = true;

function drill(d){ d.data.open = !d.data.open; update(d.data.uid); }
function fan(d){ d.data.fanned = !d.data.fanned; update((d.data.parent||d.data).uid); }
function merge(d){ if(d.data.ref){ d.data.ref.fanned=false; update((d.data.parent||d.data).uid); } }

// nearest ancestor (up the path to root) sharing this hex -> recursive back-edge
function recAncestor(d){
  for(const a of d.ancestors().slice(1)) if(a.data.hex===d.data.hex) return a;
  return null;
}
function panTo(n){
  const k=(d3.zoomTransform(svg.node()).k)||1;
  svg.transition().duration(650).call(zoom.transform,
    d3.zoomIdentity.translate(innerWidth/2 - n.x*k, innerHeight/3 - n.y*k).scale(k));
}
function flashRecursion(d,a){
  gFlash.selectAll("*").remove();
  const x1=d.x,y1=d.y,x2=a.x,y2=a.y, mx=Math.min(x1,x2)-200, my=(y1+y2)/2;
  const p=gFlash.append("path").attr("class","backedge")
    .attr("d",`M${x1},${y1} Q${mx},${my} ${x2},${y2}`).attr("marker-end","url(#recarrow)");
  const len=(p.node().getTotalLength&&p.node().getTotalLength())||700;
  p.attr("stroke-dasharray",len).attr("stroke-dashoffset",len)
    .transition().duration(650).attr("stroke-dashoffset",0)
    .transition().delay(1600).duration(600).style("opacity",0).remove();
  gFlash.append("circle").attr("class","recring").attr("cx",x2).attr("cy",y2)
    .attr("r",12).style("opacity",.9)
    .transition().duration(950).attr("r",92).style("opacity",0).remove();
  // briefly pulse the target node's card
  const tg=gNode.selectAll("g.node").filter(n=>n.data.uid===a.data.uid);
  tg.classed("pulse",true); setTimeout(()=>tg.classed("pulse",false),1400);
}
function recurseJump(d){ const a=recAncestor(d); if(a){ flashRecursion(d,a); panTo(a); } }

function update(sourceUid){
  gFlash.selectAll("*").remove();
  const h = d3.hierarchy(treeRoot, kids);
  tree(h);
  const nodes = h.descendants(), links = h.links();
  document.getElementById("counts").textContent =
    `${nodes.length} shown \u00b7 ${Object.keys(META).length} functions`;

  const o = pos.get(sourceUid) || {x:0,y:0};
  const linkGen = d3.linkVertical().x(d=>d.x).y(d=>d.y);
  const originLink = {source:{x:o.x,y:o.y}, target:{x:o.x,y:o.y}};

  const link = gLink.selectAll("path.link").data(links, d=>d.target.data.uid);
  const linkEnter = link.enter().append("path")
    .attr("class", d => "link" + (d.target.data.site ? " tosite":""))
    .attr("d", linkGen(originLink));
  const linkAll = link.merge(linkEnter)
    .attr("class", d => "link" + (d.target.data.site ? " tosite":""));
  (first ? linkAll : linkAll.transition().duration(420)).attr("d", linkGen);
  (first ? link.exit() : link.exit().transition().duration(420).attr("d", linkGen(originLink))).remove();

  const node = gNode.selectAll("g.node").data(nodes, d=>d.data.uid);
  const enter = node.enter().append("g")
    .attr("transform", `translate(${o.x},${o.y})`)
    .on("click", (e,d)=>{ e.stopPropagation(); if(recAncestor(d)) recurseJump(d); else drill(d); });

  enter.append("rect").attr("class","stripe").attr("x",-BOX_W/2).attr("y",-BOX_H/2)
    .attr("width",4).attr("height",BOX_H).attr("rx",2);
  enter.append("rect").attr("class","card").attr("x",-BOX_W/2).attr("y",-BOX_H/2)
    .attr("width",BOX_W).attr("height",BOX_H).attr("rx",9);
  const tx=-BOX_W/2+14;
  enter.append("text").attr("class","hex").attr("x",tx).attr("y",-15);
  enter.append("text").attr("class","l2").attr("x",tx).attr("y",1);
  enter.append("text").attr("class","l3").attr("x",tx).attr("y",15);
  enter.append("text").attr("class","drill").attr("x",BOX_W/2-12).attr("y",-13);

  // fan pill (function nodes with >1 callsite) — created lazily per node in merge step
  const all = enter.merge(node);
  (first ? all : all.transition().duration(420)).attr("transform", d=>`translate(${d.x},${d.y})`);

  all.attr("class", d=>{
    let c="node";
    const rec = d.depth>0 && recAncestor(d);
    if(d.depth===0) c+=" is-root";
    else if(rec) c+=" is-recursive";
    else if(d.data.site) c+=" is-site";
    if(d.data.open) c+=" expanded";
    return c;
  });
  all.select(".stripe").attr("fill", d=>{
    if(d.depth===0) return "var(--root)";
    if(d.depth>0 && recAncestor(d)) return "var(--rec)";
    if(d.data.site) return "var(--site)";
    return calleeCount(d.data.hex) ? "var(--branch)" : "var(--leaf)";
  });
  all.select(".hex").text(d=> d.depth===0 ? d.data.hex+"  \u2022 root" : d.data.hex);
  all.select(".l2").text(d=>{
    if(d.data.site) return "\u21b3 called @ "+(d.data.site.p||"?");
    return defMeta(d.data.hex).f;
  });
  all.select(".l3").text(d=>{
    if(d.data.site) return d.data.site.f;
    return defMeta(d.data.hex).p || "\u2014";
  });
  all.select(".drill").text(d=>{
    if(d.depth>0 && recAncestor(d)) return "\u21bb";   // recursive: jump to ancestor
    const c=calleeCount(d.data.hex); if(!c) return "";
    return d.data.open ? "\u25be "+c : "\u25b8 "+c;   // closed/open callees
  });

  // fan pill + merge handle (data-join inside each node)
  all.each(function(d){
    const g=d3.select(this);
    const showFan = !d.data.site && d.data.sites.length>1;
    let fanG=g.select(".fan");
    if(showFan){
      if(fanG.empty()){
        fanG=g.append("g").attr("class","fan");
        fanG.append("rect").attr("rx",6).attr("height",16);
        fanG.append("text");
      }
      fanG.on("click",(e)=>{ e.stopPropagation(); fan(d); });
      const label="\u2347 "+d.data.sites.length;
      const w=18+label.length*6;
      fanG.select("rect").attr("x",BOX_W/2-w-6).attr("y",BOX_H/2-10).attr("width",w);
      fanG.select("text").attr("x",BOX_W/2-w/2-6).attr("y",BOX_H/2-2).text(label);
    } else fanG.remove();

    let mg=g.select(".merge");
    if(d.data.site){
      if(mg.empty()){
        mg=g.append("g").attr("class","merge");
        mg.append("rect").attr("rx",6).attr("height",16);
        mg.append("text");
      }
      mg.on("click",(e)=>{ e.stopPropagation(); merge(d); });
      const label="\u293a merge", mw=14+label.length*6, mx=BOX_W/2-mw-6, my=BOX_H/2-10;
      mg.select("rect").attr("x",mx).attr("y",my).attr("width",mw);
      mg.select("text").attr("x",mx+mw/2).attr("y",my+8).text(label);
    } else mg.remove();
  });

  (first ? node.exit() : node.exit().transition().duration(420)
      .attr("transform",`translate(${o.x},${o.y})`).style("opacity",0)).remove();

  nodes.forEach(d=> pos.set(d.data.uid,{x:d.x,y:d.y}));
  first=false;
}

function fit(){
  const b=gNode.node().getBBox(), fw=innerWidth, fh=innerHeight, pad=90;
  const k=Math.min(1.8, Math.min((fw-pad)/Math.max(b.width,1),(fh-pad)/Math.max(b.height,1)));
  svg.transition().duration(500).call(zoom.transform,
    d3.zoomIdentity.translate(fw/2-(b.x+b.width/2)*k, fh/2-(b.y+b.height/2)*k).scale(k));
}
function center(animate){
  const t=d3.zoomIdentity.translate(innerWidth/2,150).scale(1);
  if(animate) svg.transition().duration(500).call(zoom.transform,t);
  else svg.call(zoom.transform,t);
}

document.getElementById("fit").onclick=fit;
document.getElementById("reset").onclick=()=>{
  treeRoot.open=false; treeRoot._dedup=null; update(treeRoot.uid); center(true);
};

update(treeRoot.uid);
center(false);
</script>
</body>
</html>
"""


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    in_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else "explorer.html"
    with open(in_path, encoding="utf-8") as f:
        files, meta, edges = parse(f.read())
    root = find_root(meta, edges)
    html = (HTML
            .replace("__FILES__", json.dumps(files, separators=(",", ":")))
            .replace("__META__", json.dumps(meta, separators=(",", ":")))
            .replace("__EDGES__", json.dumps(edges, separators=(",", ":")))
            .replace("__ROOT__", root))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    ncalls = sum(len(v) for v in edges.values())
    print(f"wrote {out_path}")
    print(f"  root {root}  ({defmeta_str(meta, files, root)})")
    print(f"  {len(meta)} functions, {ncalls} callsites, {len(files)} distinct files")


def defmeta_str(meta, files, h):
    m = meta.get(h)
    if not m: return "?"
    return f"{files[m[0]]} {m[1]}"


if __name__ == "__main__":
    main()
