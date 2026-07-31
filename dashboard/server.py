"""Attribution dashboard v3 - "Fund Terminal" (Bloomberg-density, light).

Design contract (persona: rigorous quant):
- statistics forward: every figure carries n / p / CI; uncertainty is displayed
- the pre-registered experiment (H-A/H-B/H-C) is the centerpiece; H-A's
  effect/CI/p-value are read verbatim from state.s3_vs_s1 (server-computed,
  Newey-West) - the client NEVER re-derives a significance test locally
- every number on screen is derivable from state.json (no fabrication); a
  panel with insufficient data says so instead of inventing history
- tabular/monospace numerals, flat light theme, book-drill-down sheet
Single stdlib HTTP server + Chart.js CDN; no build step.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<!-- QT-V3-IBKR -->
<title>Systematic Fund - Fund Terminal</title><link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box}
html,body{margin:0}
body{background:#eef1f5;color:#1a2233;font-family:'IBM Plex Sans',system-ui,sans-serif;font-variant-numeric:tabular-nums}
.wrap{max-width:1360px;margin:0 auto;padding:14px 16px 40px}
.card{background:#fff;border:1px solid #e3e8ef;border-radius:10px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.num{font-family:'IBM Plex Mono',ui-monospace,monospace}
.lbl{font-size:9.5px;letter-spacing:.7px;color:#94a3b8;text-transform:uppercase}
.muted{color:#94a3b8}.sec{color:#64748b}.pos{color:#15803d}.neg{color:#b91c1c}.navy{color:#0b2545}.amber{color:#b45309}.violet{color:#7c3aed}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:500;padding:3px 9px;border-radius:20px;border:1px solid transparent}
.chip.g{background:#e7f3ec;color:#15803d;border-color:#cfe6d8}.chip.r{background:#fdeaea;color:#b91c1c;border-color:#f6c9c7}
.chip.gray{background:#eef1f5;color:#475569;border-color:#e3e8ef}.chip.a{background:#fdf3e3;color:#b45309;border-color:#f2e2c4}
.dot{width:6px;height:6px;border-radius:50%;display:inline-block}
.kpi{padding:11px 13px}.kpi .v{font-family:'IBM Plex Mono';font-size:19px;font-weight:600;margin-top:4px}
.kpi .s{font-size:10px;color:#94a3b8;margin-top:2px}
.h{font-size:13px;font-weight:600;color:#0b2545}
.bar{flex:1;height:6px;background:#eef1f5;border-radius:3px;overflow:hidden}.bar>div{height:100%}
.util{height:7px;background:#eef1f5;border-radius:4px;overflow:hidden}.util>div{height:100%;border-radius:4px}
table{width:100%;border-collapse:collapse;font-size:12px}
#bookTbl{min-width:560px}
th{font-size:9.5px;color:#94a3b8;text-transform:uppercase;letter-spacing:.5px;font-weight:600;text-align:right;padding:5px 6px;border-bottom:1px solid #e3e8ef}
th:first-child,td:first-child{text-align:left}
td{padding:8px 6px;border-bottom:1px solid #f1f4f8;text-align:right;font-family:'IBM Plex Mono'}
tr.bookrow{cursor:pointer}tr.bookrow:hover{background:#f5f7fb}
button.tg{background:#fff;border:1px solid #e3e8ef;color:#64748b;font-family:'IBM Plex Mono';font-size:11px;padding:4px 10px;border-radius:6px;cursor:pointer}
button.tg.on{background:#4f5fe0;color:#fff;border-color:#4f5fe0}
.ci-wrap{position:relative;height:26px;background:#f1f4f8;border:1px solid #e3e8ef;border-radius:6px;margin:8px 0 4px}
.ci-zero{position:absolute;top:0;bottom:0;width:1px;background:#94a3b8;left:50%}
.ci-int{position:absolute;top:9px;height:6px;background:#c7cffa;border-radius:3px}
.ci-dot{position:absolute;top:7px;width:10px;height:10px;border-radius:50%;background:#4f5fe0}
.prog{height:5px;background:#eef1f5;border-radius:3px;overflow:hidden;margin-top:9px}
.prog>div{height:100%;background:#4f5fe0}
.expcard{border:1px solid #e3e8ef;border-radius:8px;padding:11px;background:#fafbfc}
.posrow{display:flex;align-items:center;gap:9px;margin-bottom:7px}
.posrow .sym{font-family:'IBM Plex Mono';width:66px;font-size:11.5px;font-weight:500;flex-shrink:0}
.posrow .diverge{flex:1;height:12px;position:relative;background:#f1f4f8;border-radius:3px}
.posrow .diverge .mid{position:absolute;top:0;bottom:0;width:1px;background:#cbd5e1;left:50%}
.posrow .diverge .fill{position:absolute;top:2px;bottom:2px;border-radius:2px}
.posrow .wpct{font-family:'IBM Plex Mono';width:56px;text-align:right;font-size:11px}
#sheetBackdrop{position:fixed;inset:0;background:rgba(15,23,42,.32);z-index:40;backdrop-filter:blur(2px);display:none}
#sheet{position:fixed;top:0;right:0;bottom:0;width:min(460px,92vw);z-index:41;background:#fff;border-left:1px solid #e3e8ef;box-shadow:-16px 0 48px rgba(16,24,40,.18);overflow-y:auto;transform:translateX(100%);transition:transform .22s ease}
#sheet.open{transform:translateX(0)}
#sheetBackdrop.open{display:block}
.statcell{background:#fafbfc;border:1px solid #e3e8ef;border-radius:8px;padding:10px 12px}
.actrow{position:relative;margin-bottom:11px;padding-left:16px}
.actrow .adot{position:absolute;left:0;top:3px;width:7px;height:7px;border-radius:50%;border:2px solid #fff}
.opsrow{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid #f1f4f8;font-size:12px}
.haltbanner{background:#fdeaea;border:1px solid #f6c9c7;color:#b91c1c;border-radius:10px;padding:10px 16px;margin-bottom:12px;font-size:12.5px;font-weight:600;display:flex;justify-content:space-between;align-items:center;gap:10px}
.row2{display:grid;grid-template-columns:1.55fr 1fr;gap:12px;margin-bottom:12px}
.row3{display:grid;grid-template-columns:1.5fr 1fr 1fr;gap:12px;margin-bottom:12px}
.row3b{display:grid;grid-template-columns:1fr 1fr 1.2fr;gap:12px;margin-bottom:12px}
.rowActivity{display:grid;grid-template-columns:1.4fr 1fr;gap:12px;margin-bottom:12px}
.hbhc{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:9px}
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
@media (max-width:860px){
  .wrap{padding:10px 10px 32px}
  .row2,.row3,.row3b,.rowActivity{grid-template-columns:1fr}
  #corrGrid{font-size:10px}
  #ibkKpis{grid-template-columns:repeat(2,1fr) !important}
}
@media (max-width:420px){
  .hbhc{grid-template-columns:1fr}
  #nav{font-size:22px !important}
  #ibkKpis{grid-template-columns:1fr 1fr !important}
}
</style></head><body>
<div class=wrap>

<div id=haltBanner class=haltbanner style="display:none">
  <span id=haltText></span>
  <span class=num style="font-weight:400;font-size:11px">clear via scripts/clear_halt.py</span>
</div>

<!-- top bar -->
<div class=card style="display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;padding:12px 18px;margin-bottom:12px">
  <div style="min-width:220px">
    <div style="display:flex;align-items:baseline;gap:10px">
      <span style="font-size:15px;font-weight:700;letter-spacing:.3px" class=navy>MULTI-STRATEGY SYSTEMATIC FUND</span>
      <span class=num style="font-size:9.5px;letter-spacing:1.2px;color:#94a3b8;border:1px solid #e3e8ef;padding:2px 7px;border-radius:4px">PAPER · PROOF PHASE</span>
    </div>
    <div id=chips style="display:flex;gap:7px;margin-top:9px;flex-wrap:wrap"></div>
  </div>
  <div style="text-align:right">
    <div id=nav class="num navy" style="font-size:27px;font-weight:600;letter-spacing:-.5px">—</div>
    <div style="display:flex;gap:9px;justify-content:flex-end;align-items:center;margin-top:5px">
      <span id=kDayHeader class="num" style="font-size:12px;font-weight:600">—</span>
      <span id=pulse style="width:7px;height:7px;border-radius:50%;background:#15803d;display:inline-block"></span>
      <span style="font-size:11px;font-weight:600" class=pos>LIVE</span>
      <span id=clk class="num" style="font-size:11px;color:#94a3b8"></span>
    </div>
  </div>
</div>

<!-- KPI strip -->
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:9px;margin-bottom:12px" id=kpis></div>

<!-- charts + experiment -->
<div class=row2>
  <div class=card style="padding:14px 16px;min-width:0">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px">
      <div class=h>Four-book horse race <span class=muted style="font-weight:400;font-size:11px">· daily closes</span></div>
      <div style="display:flex;gap:5px;align-items:center">
        <button class="tg on" id=mUsd>$</button><button class=tg id=mPct>%</button>
        <span style="width:8px"></span>
        <button class=tg id=r7>7D</button><button class=tg id=r30>30D</button><button class="tg on" id=rAll>All</button>
      </div>
    </div>
    <div id=legend style="display:flex;flex-wrap:wrap;gap:14px;font-size:11px;color:#64748b;margin-bottom:8px"></div>
    <div style="position:relative;width:100%;height:270px"><canvas id=eq></canvas></div>
    <div class=lbl style="margin:14px 0 5px">Drawdown · S4 combined - governor gates at −10% / −15%</div>
    <div style="position:relative;width:100%;height:96px"><canvas id=dd></canvas></div>
  </div>

  <div class=card style="padding:14px 16px;border-left:3px solid #4f5fe0;border-radius:0 10px 10px 0;min-width:0">
    <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap">
      <div class=h>Pre-registered experiment</div>
      <span id=expVerdict class="chip gray">—</span>
    </div>
    <div class=muted style="font-size:10.5px;margin-top:3px">locked 2026-07-02 · readout at ≥60 trading days AND p&lt;0.05</div>

    <div class=expcard style="margin-top:12px">
      <div class=lbl>H-A · does the LLM book beat the control?</div>
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:6px">
        <span class="num navy" style="font-size:18px;font-weight:600" id=haEst>—</span>
        <span class="num sec" style="font-size:11px" id=haStat>—</span>
      </div>
      <div class=ci-wrap id=haCi><div class=ci-zero></div></div>
      <div style="display:flex;justify-content:space-between" class=muted>
        <span style="font-size:10px" id=haDom>—</span><span style="font-size:10px">S3−S1 daily diff · DM / Newey-West (server-side)</span>
      </div>
      <div class=prog><div id=haProg style="width:0%"></div></div>
      <div class=muted style="font-size:10px;margin-top:4px" id=haProgTxt>—</div>
    </div>

    <div class=hbhc>
      <div class=expcard>
        <div class=lbl>H-B · news book edge</div>
        <div class="num" style="font-size:16px;font-weight:600;margin-top:6px" id=hbEst>—</div>
        <div class=muted style="font-size:10px;margin-top:4px" id=hbStat>—</div>
      </div>
      <div class=expcard>
        <div class=lbl>H-C · ensemble &gt; best?</div>
        <div class="num" style="font-size:16px;font-weight:600;margin-top:6px" id=hcEst>—</div>
        <div class=muted style="font-size:10px;margin-top:4px" id=hcStat>—</div>
      </div>
    </div>

    <div style="margin-top:12px">
      <div class=lbl style="margin-bottom:6px">S3 decision audit - who is deciding? <span style="text-transform:none">(all-time)</span></div>
      <div style="display:flex;height:9px;border-radius:5px;overflow:hidden;background:#eef1f5">
        <div id=pmLlm style="background:#7c3aed;width:0%"></div><div id=pmHeld style="background:#94a3b8;width:0%"></div><div id=pmHeur style="background:#cbd5e1;width:0%"></div>
      </div>
      <div class="muted num" style="font-size:10px;margin-top:5px" id=pmTxt>—</div>
    </div>
  </div>
</div>

<!-- books + correlation + risk -->
<div class=row3>
  <div class=card style="padding:14px 16px;min-width:0">
    <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px">
      <div class=h>Books</div><div class=muted style="font-size:10.5px">click a row to drill in →</div>
    </div>
    <div class=tbl-wrap><table id=bookTbl></table></div>
  </div>
  <div class=card style="padding:14px 16px;min-width:0">
    <div class=h style="margin-bottom:3px">Cross-book correlation</div>
    <div class=muted style="font-size:10.5px;margin-bottom:12px">daily returns · lower = better diversification</div>
    <div id=corrGrid style="display:grid;grid-template-columns:auto repeat(4,1fr);gap:4px"></div>
    <div class=sec style="font-size:10.5px;margin-top:12px;line-height:1.5" id=corrNote></div>
  </div>
  <div class=card style="padding:14px 16px;min-width:0">
    <div class=h style="margin-bottom:12px">Risk &amp; limit utilization</div>
    <div id=limits></div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:14px;padding-top:12px;border-top:1px solid #e3e8ef" id=regimeCells></div>
  </div>
</div>

<!-- longs / shorts / attribution -->
<div class=row3b>
  <div class=card style="padding:14px 16px;min-width:0">
    <div class=h style="margin-bottom:11px"><span class=pos>Top longs</span> <span class=muted style="font-size:11px;font-weight:400">· S4 net weight</span></div>
    <div id=longs></div>
  </div>
  <div class=card style="padding:14px 16px;min-width:0">
    <div class=h style="margin-bottom:11px"><span class=neg>Top shorts</span> <span class=muted style="font-size:11px;font-weight:400">· S4 net weight</span></div>
    <div id=shorts></div>
  </div>
  <div class=card style="padding:14px 16px;min-width:0">
    <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:11px">
      <div class=h>P&amp;L attribution <span class=muted style="font-size:11px;font-weight:400">· S4, since last daily bar</span></div>
      <span id=attrDay class="num" style="font-size:12px;font-weight:600">—</span>
    </div>
    <div id=attrib></div>
  </div>
</div>

<!-- IBKR paper mirror - live account vs S4 slice targets -->
<div class=card style="padding:14px 16px;margin-bottom:12px;min-width:0">
  <div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:10px">
    <div>
      <div class=h>IBKR paper mirror</div>
      <div class=muted style="font-size:10.5px;margin-top:2px">live broker account · refreshed every tick · vs S4 equity slice targets</div>
    </div>
    <div id=ibkHead class="num" style="font-size:11.5px;color:#64748b">—</div>
  </div>
  <div id=ibkKpis style="display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-bottom:12px"></div>
  <div class=tbl-wrap>
    <table id=ibkTbl style="min-width:640px">
      <thead><tr>
        <th>Symbol</th><th>Shares</th><th>Price</th><th>Mkt value</th>
        <th>Weight</th><th>Target</th><th>Drift</th>
      </tr></thead>
      <tbody id=ibkBody></tbody>
    </table>
  </div>
  <div style="margin-top:12px;padding-top:10px;border-top:1px solid #e3e8ef">
    <div class=muted style="font-size:10.5px;margin-bottom:6px">Recent fills (today)</div>
    <div id=ibkFills class="num" style="font-size:11px;color:#475569;line-height:1.55">—</div>
  </div>
</div>

<!-- activity + ops -->
<div class=rowActivity>
  <div class=card style="padding:14px 16px;min-width:0">
    <div class=h style="margin-bottom:12px">Activity log</div>
    <div style="position:relative" id=activity></div>
  </div>
  <div class=card style="padding:14px 16px;min-width:0">
    <div class=h style="margin-bottom:12px">System &amp; data health</div>
    <div id=ops></div>
  </div>
</div>

<div class=muted style="font-size:10px;text-align:center;padding:6px 0 10px">
paper money · every figure derives from state.json · pre-registered rule: no edge/superiority claim before 60 trading days AND p&lt;0.05 (RESEARCH_LOG E7) · auto-refresh 60s
</div>
</div>

<div id=sheetBackdrop></div>
<div id=sheet>
  <div style="display:flex;justify-content:space-between;align-items:flex-start;padding:18px 20px;border-bottom:1px solid #e3e8ef;position:sticky;top:0;background:#fff">
    <div>
      <div style="display:flex;align-items:center;gap:9px">
        <span id=shSwatch style="width:11px;height:11px;border-radius:3px"></span>
        <span id=shName style="font-size:16px;font-weight:700" class=navy>—</span>
      </div>
      <div id=shDesc class=sec style="font-size:11.5px;margin-top:5px;max-width:320px;line-height:1.5"></div>
    </div>
    <button id=shClose style="background:#f1f4f8;border:1px solid #e3e8ef;color:#64748b;width:28px;height:28px;border-radius:7px;cursor:pointer;font-size:15px">×</button>
  </div>
  <div style="padding:18px 20px">
    <div id=shStats style="display:grid;grid-template-columns:1fr 1fr;gap:9px"></div>
    <div class=lbl style="margin:18px 0 6px">Equity curve · full history</div>
    <div style="position:relative;width:100%;height:150px"><canvas id=spark></canvas></div>
    <div class=lbl style="margin:18px 0 8px" id=shPosHdr>Positions</div>
    <div id=shPositions></div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const COL={s1:'#94a3b8',s2:'#0891b2',s3:'#7c3aed',s4:'#0b2545',s5:'#ca8a04'};
const NM={s1:'S1 Quant',s2:'S2 News',s3:'S3 LLM',s4:'S4 Combined',s5:'S5 Event'};
const SHORT={s1:'S1',s2:'S2',s3:'S3',s4:'S4',s5:'S5'};
const DESC={
 s1:'Deterministic quant book - the control group. Price/volume/factor signals, no LLM. If the LLM books cannot beat this, the LLM only adds cost.',
 s2:'News/event book. Tiered lexicon / LLM scoring over RSS + SEC EDGAR 8-Ks, vol-normalized like every other book.',
 s3:'LLM discretionary book. Frontier PM inside a hard risk wrapper (pm_source audited). Falls back to Ollama/heuristic when budget-capped or unavailable.',
 s4:'Risk-parity ensemble of S1+S2+S3, vol-normalized to a shared 10% ex-ante target. The book the fund actually reports as its combined result.',
 s5:'Event-driven sleeve - forward shadow of the CONFIRMED 8k-drift edge (E18). Long positive-reaction / short negative-reaction 8-Ks, standalone (not in the S4 ensemble). Zero further optimization.'
};
// the four core race books, plus S5 only once it exists in state (enabled)
function BOOKS(s){return ['s1','s2','s3','s4'].concat((s&&s.systems&&s.systems.s5)?['s5']:[]);}
let eqChart=null, ddChart=null, sparkChart=null, MODE='usd', RANGE='all', LAST=null, ACTIVE_BOOK=null;
const MAX_POS_ROWS=60;

const fmt$=n=>(n<0?'−$':'$')+Math.abs(Math.round(n)).toLocaleString();
const pct=(n,d=2)=>(n>=0?'+':'')+(n*100).toFixed(d)+'%';
const gpos='#15803d', gneg='#b91c1c', navy='#0b2545';
function daily(hist){const m=new Map();for(const p of hist||[])m.set(p[0].slice(0,10),p[1]);const d=[...m.keys()].sort();return {d,v:d.map(k=>m.get(k))};}
function rets(v){const r=[];for(let i=1;i<v.length;i++)r.push(v[i]/v[i-1]-1);return r;}
function mean(a){return a.length?a.reduce((x,y)=>x+y,0)/a.length:0;}
function std(a){if(a.length<2)return 0;const m=mean(a);return Math.sqrt(a.reduce((x,y)=>x+(y-m)**2,0)/(a.length-1));}
function sharpe(v){const r=rets(v);if(r.length<8)return null;const sd=std(r)||1e-12;return mean(r)/sd*Math.sqrt(252);}
function rvol(v){const r=rets(v);if(r.length<8)return null;return std(r)*Math.sqrt(252);}
function maxdd(v){let pk=v[0]||0,d=0;for(const x of v){pk=Math.max(pk,x);d=Math.min(d,x/pk-1);}return d;}
function ddSeries(v){const out=[];let pk=v[0]||0;for(const x of v){pk=Math.max(pk,x);out.push((x/pk-1)*100);}return out;}
function corr(a,b){const n=Math.min(a.length,b.length);if(n<8)return null;a=a.slice(-n);b=b.slice(-n);
 const ma=mean(a),mb=mean(b);let num=0,da=0,db=0;
 for(let i=0;i<n;i++){num+=(a[i]-ma)*(b[i]-mb);da+=(a[i]-ma)**2;db+=(b[i]-mb)**2;}
 const denom=Math.sqrt(da*db);return denom<1e-15?null:num/denom;}
function slice(arr,n){return RANGE==='all'?arr:arr.slice(-n);}
function rangeN(){return RANGE==='7'?8:RANGE==='30'?31:1e9;}
function gross(w){return Object.values(w||{}).reduce((a,b)=>a+Math.abs(b),0);}
function timeAgo(iso){if(!iso)return '—';const s=(Date.now()-new Date(iso).getTime())/1000;
 if(s<90)return Math.round(s)+'s ago';if(s<5400)return Math.round(s/60)+'m ago';return Math.round(s/3600)+'h ago';}
function corrCell(v){
 if(v==null)return {bg:'#f1f4f8',fg:'#94a3b8',t:'—'};
 if(v>=0.999)return {bg:'#eef1f5',fg:navy,t:v.toFixed(2)};
 if(v>=0.7)return {bg:'#fdeaea',fg:gneg,t:v.toFixed(2)};
 if(v>=0.4)return {bg:'#fdf3e3',fg:'#b45309',t:v.toFixed(2)};
 if(v>=0.15)return {bg:'#f4f7f2',fg:'#4d7c4f',t:v.toFixed(2)};
 return {bg:'#e7f3ec',fg:gpos,t:v.toFixed(2)};
}

function bookStats(s,k){
 const dk=daily((s.equity_history||{})[k]);
 const sysd=(s.systems&&s.systems[k])||{};
 const equity=sysd.equity!=null?sysd.equity:(dk.v.at(-1)||s.nav0);
 return {dates:dk.d, eq:dk.v, ret:equity/s.nav0-1, vol:rvol(dk.v), sharpe:sharpe(dk.v),
  dd:maxdd(dk.v), weights:sysd.weights||{}, equity};
}

function chip(text,cls){return '<span class="chip '+cls+'"><span class=dot style="background:currentColor"></span>'+text+'</span>';}
function statsMap(s){const m={};for(const k of BOOKS(s))m[k]=bookStats(s,k);return m;}

function render(s){
 const nav0=s.nav0;
 const B=statsMap(s);
 const nav=B.s4.equity;
 document.getElementById('nav').textContent=fmt$(nav);

 const day=B.s4.eq.length>=2?B.s4.eq.at(-1)-B.s4.eq.at(-2):0;
 const dayColor=day>=0?gpos:gneg;
 const dayTxt=(day>=0?'+':'−')+'$'+Math.abs(Math.round(day)).toLocaleString();
 document.getElementById('kDayHeader').textContent=dayTxt;
 document.getElementById('kDayHeader').style.color=dayColor;

 // halt banner
 const halt=s.halt_latched;
 const hb=document.getElementById('haltBanner');
 if(halt){hb.style.display='flex';document.getElementById('haltText').textContent=
   '🛑 HALT LATCHED since '+(halt.ts||'').slice(0,16).replace('T',' ')+' UTC - all books flat. Reason: '+(halt.reason||'—');}
 else hb.style.display='none';

 // chips
 const rg=s.regime||{};
 const chips=[];
 chips.push(chip('regime '+(rg.trend==='risk_on'?'risk-on':'risk-off')+' · VIX '+(rg.vix!=null?rg.vix.toFixed(1):'—'),
   rg.trend==='risk_on'?'g':'a'));
 if(halt) chips.push(chip('governor · HALTED','r'));
 else if((s.last_actions||[]).length) chips.push(chip('governor · '+s.last_actions[0].slice(0,40),'a'));
 else chips.push(chip('governor · all clear','g'));
 const cutoff=Date.now()-24*3600*1000;
 const inc24=(s.data_incidents||[]).filter(i=>new Date(i.ts).getTime()>=cutoff).length;
 chips.push(chip('incidents (24h) '+inc24, inc24?'a':'gray'));
 const pmSrc=s.s3_pm_source||'heuristic';
 const pmLabel={anthropic:'anthropic (frontier)',ollama:'ollama (local)',heuristic:'heuristic (fallback)',held:'held (no new decision)'}[pmSrc]||pmSrc;
 chips.push(chip('S3 brain · '+pmLabel, (pmSrc==='anthropic'||pmSrc==='ollama'||pmSrc==='held')?'g':'a'));
 document.getElementById('chips').innerHTML=chips.join('');

 // KPIs
 const inc=nav/nav0-1;
 const bm=daily(s.benchmark);
 const spyRet=bm.v.length>=2?(bm.v.at(-1)/bm.v[0]-1):null;
 const vsS=spyRet==null?null:(inc-spyRet);
 const n=Math.max(B.s4.eq.length-1,0);
 const kpis=[
  {lbl:'Day P&L',val:dayTxt,color:dayColor,sub:'vs prior daily close'},
  {lbl:'Inception',val:pct(inc),color:inc>=0?gpos:gneg,sub:'since '+(B.s4.dates[0]||'—')},
  {lbl:'vs SPY',val:vsS==null?'n/a':pct(vsS),color:vsS==null?navy:(vsS>=0?gpos:gneg),sub:'same $ base'},
  {lbl:'Sharpe (daily)',val:B.s4.sharpe==null?'n/a':B.s4.sharpe.toFixed(2),color:navy,sub:'n='+n+'d'+(n<60?' · still short':'')},
  {lbl:'Ann. vol',val:B.s4.vol==null?'n/a':(B.s4.vol*100).toFixed(1)+'%',color:navy,sub:'target 10%'},
  {lbl:'Max DD',val:(B.s4.dd*100).toFixed(2)+'%',color:gneg,sub:'gate −10 / −15%'},
  {lbl:'Gross',val:Math.round(gross(B.s4.weights)*100)+'%',color:navy,sub:'limit 100%'},
  {lbl:'Days live',val:String(n),color:navy,sub:'last tick '+timeAgo(s.last_tick)}
 ];
 document.getElementById('kpis').innerHTML=kpis.map(k=>
  '<div class="card kpi"><div class=lbl>'+k.lbl+'</div><div class=v style="color:'+k.color+'">'+k.val+'</div><div class=s>'+k.sub+'</div></div>').join('');

 // legend
 document.getElementById('legend').innerHTML=BOOKS(s).map(k=>
  '<span style="display:inline-flex;align-items:center;gap:6px"><b style="display:inline-block;width:15px;height:3px;background:'+COL[k]+'"></b>'+NM[k]+'</span>').join('')
  +'<span style="display:inline-flex;align-items:center;gap:6px"><b style="display:inline-block;width:15px;height:1px;border-top:2px dashed #9aa5b1"></b>SPY</span>';

 // experiment panel - H-A verbatim from server-computed state.s3_vs_s1
 const pv=s.s3_vs_s1||{};
 const haEst=document.getElementById('haEst'), haStat=document.getElementById('haStat');
 if(pv.mean_daily_bps==null){haEst.textContent='collecting';haEst.style.color=navy;haStat.textContent='n='+(pv.n||0)+'d (need ≥10 to compute)';}
 else{haEst.textContent=(pv.mean_daily_bps>=0?'+':'')+pv.mean_daily_bps.toFixed(1)+' bps/d';
  haEst.style.color=pv.mean_daily_bps>=0?gpos:gneg;
  haStat.textContent='p='+(pv.p_value==null?'—':pv.p_value.toFixed(3))+' · n='+pv.n+'d';}
 const ci=document.getElementById('haCi');
 ci.querySelectorAll('.ci-int,.ci-dot').forEach(e=>e.remove());
 if(pv.mean_daily_bps!=null&&pv.ci95_bps!=null){
  const dom=Math.max(40,Math.ceil((Math.abs(pv.mean_daily_bps)+pv.ci95_bps)/10)*10);
  document.getElementById('haDom').textContent='±'+dom+' bps scale';
  const mapx=v=>Math.max(0,Math.min(100,50+(v/dom)*50));
  const lo=mapx(pv.mean_daily_bps-pv.ci95_bps), hi=mapx(pv.mean_daily_bps+pv.ci95_bps);
  const it=document.createElement('div');it.className='ci-int';it.style.left=lo+'%';it.style.width=Math.max(hi-lo,1)+'%';ci.appendChild(it);
  const dot=document.createElement('div');dot.className='ci-dot';dot.style.left='calc('+mapx(pv.mean_daily_bps)+'% - 5px)';ci.appendChild(dot);
 } else document.getElementById('haDom').textContent='—';
 const prog=Math.min(100,Math.round(100*(pv.n||0)/60));
 document.getElementById('haProg').style.width=prog+'%';
 document.getElementById('haProgTxt').textContent=(pv.n||0)+' / 60 trading days to readout';
 const ev=document.getElementById('expVerdict');
 if(pv.verdict_allowed){ev.textContent='READOUT ALLOWED';ev.className='chip '+(pv.mean_daily_bps>=0?'g':'r');}
 else{ev.textContent='VERDICT LOCKED · needs ≥60d & p<0.05';ev.className='chip gray';}

 document.getElementById('hbEst').textContent=B.s2.sharpe==null?'collecting':('Sharpe '+B.s2.sharpe.toFixed(2));
 document.getElementById('hbStat').textContent='n='+Math.max(B.s2.eq.length-1,0)+'d · DSR at readout';

 const singles=[B.s1.sharpe,B.s2.sharpe,B.s3.sharpe].filter(x=>x!=null&&isFinite(x));
 const best=singles.length?Math.max(...singles):null;
 const hcEst=document.getElementById('hcEst');
 if(B.s4.sharpe==null||best==null){hcEst.textContent='collecting';hcEst.style.color=navy;document.getElementById('hcStat').textContent='';}
 else{const d=B.s4.sharpe-best;hcEst.textContent='Δ '+(d>=0?'+':'')+d.toFixed(2);hcEst.style.color=d>=0?gpos:gneg;
  document.getElementById('hcStat').textContent='S4 '+B.s4.sharpe.toFixed(2)+' vs best '+best.toFixed(2);}

 // S3 decision audit - three real buckets (anthropic+ollama / held / heuristic)
 const pmc=s.s3_pm_counts||{};
 const llmN=(pmc.anthropic||0)+(pmc.ollama||0), heldN=pmc.held||0, heurN=pmc.heuristic||0;
 const totPm=llmN+heldN+heurN;
 const llmPct=totPm?100*llmN/totPm:0, heldPct=totPm?100*heldN/totPm:0, heurPct=totPm?100*heurN/totPm:0;
 document.getElementById('pmLlm').style.width=llmPct+'%';
 document.getElementById('pmHeld').style.width=heldPct+'%';
 document.getElementById('pmHeur').style.width=heurPct+'%';
 document.getElementById('pmTxt').textContent='anthropic '+(pmc.anthropic||0)+' · ollama '+(pmc.ollama||0)
  +' · held '+heldN+' · heuristic '+heurN+' · data: '+(s.data_provider||'—');

 // books table
 let rows='<tr><th>Book</th><th>Equity</th><th>Ret</th><th>Vol</th><th>Sharpe</th><th>MaxDD</th><th>Pos</th><th>Gross</th></tr>';
 for(const k of BOOKS(s)){
  const b=B[k];
  rows+='<tr class=bookrow data-book="'+k+'"><td><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:'+COL[k]+';margin-right:8px;vertical-align:middle" class=num></span><span style="font-family:IBM Plex Sans">'+NM[k]+'</span></td>'
   +'<td>'+fmt$(b.equity)+'</td>'
   +'<td style="color:'+(b.ret>=0?gpos:gneg)+'">'+pct(b.ret)+'</td>'
   +'<td class=sec>'+(b.vol==null?'—':(b.vol*100).toFixed(1)+'%')+'</td>'
   +'<td>'+(b.sharpe==null?'—':b.sharpe.toFixed(2))+'</td>'
   +'<td style="color:'+gneg+'">'+(b.dd*100).toFixed(1)+'%</td>'
   +'<td class=sec>'+Object.keys(b.weights).length+'</td>'
   +'<td class=sec>'+Math.round(gross(b.weights)*100)+'%</td></tr>';
 }
 document.getElementById('bookTbl').innerHTML=rows;
 document.querySelectorAll('#bookTbl tr.bookrow').forEach(tr=>tr.onclick=()=>openBook(tr.dataset.book));

 // correlation matrix (descriptive Pearson corr - NOT a hypothesis test)
 const bk=BOOKS(s);
 const retSeries={};for(const k of bk)retSeries[k]=rets(B[k].eq);
 const cg=document.getElementById('corrGrid');
 cg.style.gridTemplateColumns='auto repeat('+bk.length+',1fr)';
 let grid='<div></div>';
 for(const k of bk) grid+='<div class=num style="font-size:10px;text-align:center;font-weight:600;color:'+COL[k]+'">'+SHORT[k]+'</div>';
 for(const a of bk){
  grid+='<div class=num style="font-size:10px;display:flex;align-items:center;font-weight:600;color:'+COL[a]+'">'+SHORT[a]+'</div>';
  for(const b of bk){
   const v=a===b?1.0:corr(retSeries[a],retSeries[b]);
   const cc=corrCell(v);
   grid+='<div class=num style="aspect-ratio:1;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;background:'+cc.bg+';color:'+cc.fg+'">'+cc.t+'</div>';
  }
 }
 document.getElementById('corrGrid').innerHTML=grid;
 const s1s4=corr(retSeries.s1,retSeries.s4), s2s4=corr(retSeries.s2,retSeries.s4);
 document.getElementById('corrNote').textContent=
  (s1s4==null?'Insufficient history yet for a stable correlation matrix (need ≥8 overlapping daily returns per pair).'
   :'S1–S4 '+s1s4.toFixed(2)+(s2s4==null?'':' · S2–S4 '+s2s4.toFixed(2))+'. Lower off-diagonal values mean more diversification benefit in the risk-parity blend.');

 // risk & limits
 const w4=B.s4.weights;
 const maxw=Object.values(w4).reduce((a,b)=>Math.max(a,Math.abs(b)),0);
 const curDD=Math.abs(B.s4.dd);
 const g4=gross(w4);
 function utilRow(name,val,limit,txt){
  const u=limit>0?val/limit:0;const color=u>0.8?'#b45309':navy;
  return '<div style="margin-bottom:12px"><div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px">'
   +'<span class=sec>'+name+'</span><span class=num style="color:#1a2233">'+txt+'</span></div>'
   +'<div class=util><div style="width:'+Math.min(u*100,100)+'%;background:'+color+'"></div></div></div>';
 }
 document.getElementById('limits').innerHTML=
  utilRow('Gross exposure',g4,1.0,Math.round(g4*100)+'% / 100%')
  +utilRow('Largest position |w|',maxw,0.05,(maxw*100).toFixed(1)+'% / 5%')
  +utilRow('Drawdown vs halt gate',curDD,0.15,(curDD*100).toFixed(1)+'% / 15%');
 const cell=(l,v)=>'<div><div class=lbl>'+l+'</div><div class="num navy" style="font-size:15px;font-weight:600;margin-top:3px">'+v+'</div></div>';
 document.getElementById('regimeCells').innerHTML=
  cell('VIX pct (1y)',rg.vix_percentile!=null?Math.round(rg.vix_percentile*100)+'%':'—')
  +cell('Breadth',rg.breadth!=null?Math.round(rg.breadth*100)+'%':'—')
  +cell('Risk scale',rg.risk_scale!=null?rg.risk_scale.toFixed(2):'—');

 // longs / shorts
 function posRows(entries,color){
  if(!entries.length)return '<div class=sec style="font-size:12px">— (cash / no positions)</div>';
  return entries.map(([sym,w])=>'<div class=posrow><span class="sym num">'+sym+'</span>'
   +'<div class=bar><div style="width:'+Math.min(Math.abs(w)/0.05,1)*100+'%;background:'+color+'"></div></div>'
   +'<span class="wpct num" style="color:'+color+'">'+(w*100).toFixed(1)+'%</span></div>').join('');
 }
 const ents=Object.entries(w4).sort((a,b)=>b[1]-a[1]);
 document.getElementById('longs').innerHTML=posRows(ents.filter(e=>e[1]>1e-4).slice(0,7),gpos);
 document.getElementById('shorts').innerHTML=posRows(ents.filter(e=>e[1]<-1e-4).slice(-7).reverse(),gneg);

 // P&L attribution (real, from state.s4_attribution - see runtime/live.py)
 document.getElementById('attrDay').textContent=dayTxt;
 document.getElementById('attrDay').style.color=dayColor;
 const attr=(s.s4_attribution||[]).slice(0,9);
 const attrEl=document.getElementById('attrib');
 if(!attr.length){
  attrEl.innerHTML='<div class=sec style="font-size:12px">No mark-to-market move recorded yet at the current price snapshot - attribution populates on the next daily bar.</div>';
 } else {
  const maxAbs=Math.max(...attr.map(r=>Math.abs(r[3])))||1;
  attrEl.innerHTML=attr.map(([sym,w,r,usd])=>{
   const pos=usd>=0, width=(Math.abs(usd)/maxAbs)*48, color=pos?gpos:gneg;
   const left=pos?50:(50-width);
   return '<div class=posrow><span class="sym num" style="width:60px">'+sym+'</span>'
    +'<span class=muted style="font-size:9.5px;width:34px">'+(w>=0?'LONG':'SHORT')+'</span>'
    +'<div class="bar" style="height:14px;position:relative;background:#f1f4f8"><div class=mid style="position:absolute;top:0;bottom:0;width:1px;background:#cbd5e1;left:50%"></div>'
    +'<div style="position:absolute;top:2px;bottom:2px;border-radius:2px;background:'+color+';left:'+left.toFixed(1)+'%;width:'+width.toFixed(1)+'%"></div></div>'
    +'<span class="wpct num" style="color:'+color+'">'+(pos?'+$':'−$')+Math.abs(Math.round(usd)).toLocaleString()+'</span></div>';
  }).join('');
 }

 // activity log - built only from real signals (no invented entries)
 const events=[];
 if(halt) events.push({ts:halt.ts,text:'🛑 Halt latched',tag:halt.reason||'',dot:gneg});
 if(s.last_rebalance) events.push({ts:s.last_rebalance,text:'Rebalance - '+(s.last_rebalance_reason||'scheduled'),
   tag:'S3 source: '+(s.s3_pm_source||'—')+' · '+Object.keys(w4).length+' S4 names',dot:navy});
 (s.last_actions||[]).forEach(a=>events.push({ts:s.last_tick,text:'Governor: '+a,tag:'this tick',dot:'#d97706'}));
 (s.data_incidents||[]).slice(-6).reverse().forEach(i=>events.push({ts:i.ts,text:i.kind,tag:i.detail,dot:'#b45309'}));
 events.sort((a,b)=>new Date(b.ts)-new Date(a.ts));
 const actEl=document.getElementById('activity');
 if(!events.length){actEl.innerHTML='<div class=sec style="font-size:12px">No activity recorded yet.</div>';}
 else actEl.innerHTML='<div style="position:absolute;left:3px;top:3px;bottom:3px;width:1px;background:#e3e8ef"></div>'
  +events.slice(0,10).map(e=>'<div class=actrow><span class=adot style="background:'+e.dot+'"></span>'
   +'<div style="display:flex;justify-content:space-between;gap:10px"><span style="font-size:12px">'+e.text+'</span>'
   +'<span class=num style="font-size:10px;color:#94a3b8;white-space:nowrap">'+timeAgo(e.ts)+'</span></div>'
   +'<div class=muted style="font-size:10.5px;margin-top:1px">'+e.tag+'</div></div>').join('');

 // IBKR paper mirror - live broker positions vs S4 slice targets
 const ibk=s.ibkr;
 const ibkHead=document.getElementById('ibkHead');
 const ibkKpis=document.getElementById('ibkKpis');
 const ibkBody=document.getElementById('ibkBody');
 const ibkFills=document.getElementById('ibkFills');
 if(!ibk || !ibk.account){
  if(ibkHead) ibkHead.textContent='not connected';
  if(ibkKpis) ibkKpis.innerHTML='';
  if(ibkBody) ibkBody.innerHTML='<tr><td colspan=7 class=muted style="text-align:left">No IBKR state yet.</td></tr>';
  if(ibkFills) ibkFills.textContent='—';
 } else {
  const book=ibk.book||{};
  const staleMin=ibk.last_refresh?((Date.now()-new Date(ibk.last_refresh))/6e4):null;
  const staleWarn=staleMin!=null && staleMin>90;
  if(ibkHead) ibkHead.innerHTML=ibk.account
   +' · refresh '+timeAgo(ibk.last_refresh||ibk.last_sync)
   +(ibk.last_sync?' · last trade sync '+timeAgo(ibk.last_sync):'')
   +(staleWarn?' · <span class=amber>STALE</span>':'')
   +(ibk.refresh_error?' · <span class=amber>read err: '+String(ibk.refresh_error).slice(0,40)+'</span>':'');
  const kpis=[
   ['NAV', ibk.nav!=null?'$'+Math.round(ibk.nav).toLocaleString():'—'],
   ['Positions', String(ibk.n_positions??(ibk.positions||[]).length)],
   ['Gross', book.gross!=null?(book.gross*100).toFixed(1)+'%':'—'],
   ['Net', book.net!=null?((book.net>=0?'+':'')+(book.net*100).toFixed(1)+'%'):'—'],
   ['Cash', book.cash_frac!=null?(book.cash_frac*100).toFixed(1)+'%':'—'],
   ['Max |drift|', book.max_abs_drift!=null?(book.max_abs_drift*100).toFixed(2)+'pp':'—'],
  ];
  if(ibkKpis) ibkKpis.innerHTML=kpis.map(([a,b])=>
   '<div class=statcell><div class=lbl>'+a+'</div><div class=num style="font-size:14px;font-weight:600;margin-top:3px">'+b+'</div></div>').join('');
  const rows=(ibk.positions||[]).slice(0,100);
  if(ibkBody){
   if(!rows.length) ibkBody.innerHTML='<tr><td colspan=7 class=muted style="text-align:left">No positions (flat or awaiting first refresh).</td></tr>';
   else ibkBody.innerHTML=rows.map(r=>{
    const drift=r.drift_w||0, dc=Math.abs(drift)>0.005?(drift>0?gpos:gneg):'#64748b';
    const mv=r.mv||0, mc=mv>=0?gpos:gneg;
    return '<tr>'
     +'<td class=num style="font-weight:500">'+r.symbol+'</td>'
     +'<td class=num>'+r.shares+'</td>'
     +'<td class=num>'+(r.price!=null?'$'+Number(r.price).toLocaleString(undefined,{maximumFractionDigits:2}):'—')+'</td>'
     +'<td class=num style="color:'+mc+'">'+(mv>=0?'+$':'$')+Math.round(Math.abs(mv)).toLocaleString()+'</td>'
     +'<td class=num>'+((r.weight||0)*100).toFixed(2)+'%</td>'
     +'<td class=num>'+((r.target_w||0)*100).toFixed(2)+'%</td>'
     +'<td class=num style="color:'+dc+'">'+(drift>=0?'+':'')+(drift*100).toFixed(2)+'pp</td>'
     +'</tr>';
   }).join('');
  }
  const fills=(ibk.fills||[]).slice(-12).reverse();
  if(ibkFills){
   if(!fills.length) ibkFills.textContent='No fills recorded yet.';
   else ibkFills.innerHTML=fills.map(f=>{
    const side=f.side||'';
    const col=/BOT|BUY/i.test(side)?gpos:gneg;
    return '<span style="color:'+col+'">'+side+' '+f.shares+' '+f.symbol+' @ '+Number(f.price).toFixed(2)+'</span>'
     +' <span class=muted>'+timeAgo(f.time)+'</span>';
   }).join(' · ');
  }
 }

 // ops / system health
 const tk=s.tick_stats||{};
 const budget=(s.llm_budget)||{};
 const opsRows=[
  {name:'Last tick',val:timeAgo(s.last_tick),color:navy,dot:'#22a355'},
  {name:'Ticks total',val:String(s.ticks||0),color:navy,dot:'#22a355'},
  {name:'Data provider',val:s.data_provider||'—',color:'#1a2233',dot:'#22a355'},
  {name:'Anthropic key',val:s.anthropic_active?'active':'inactive',color:s.anthropic_active?gpos:'#94a3b8',dot:s.anthropic_active?'#22a355':'#94a3b8'},
  {name:'PM calls today',val:(budget.pm||0)+' / '+(budget.pm_cap!=null?budget.pm_cap:'—'),color:'#1a2233',dot:'#22a355'},
  {name:'Scorer calls today',val:(budget.scorer||0)+' / '+(budget.scorer_cap!=null?budget.scorer_cap:'—'),color:'#1a2233',dot:'#22a355'},
  {name:'Headlines processed',val:(s.s2_news_processed||0).toLocaleString(),color:'#1a2233',dot:'#22a355'},
  {name:'Last tick duration',val:tk.secs!=null?tk.secs+'s':'—',color:'#1a2233',dot:'#22a355'},
  {name:'Halt latch',val:halt?'LATCHED':'clear',color:halt?gneg:gpos,dot:halt?'#dc4c48':'#22a355'},
 ];
 if(ibk){
  const bad=(ibk.failed&&ibk.failed.length)||(ibk.api_errors&&ibk.api_errors.length);
  opsRows.push({name:'IBKR paper '+(ibk.account||''),
   val:(ibk.nav?'$'+Math.round(ibk.nav).toLocaleString():'—')+' · '+(ibk.n_positions??'?')+' pos · '+(ibk.mode||''),
   color:'#1a2233',dot:bad?'#d97706':'#22a355'});
  opsRows.push({name:'IBKR last refresh',val:timeAgo(ibk.last_refresh||ibk.last_sync)+' · '+(ibk.n_fills_today||0)+' fills today',
   color:'#1a2233',dot:'#22a355'});
 }
 document.getElementById('ops').innerHTML=opsRows.map(o=>
  '<div class=opsrow><span class=sec>'+o.name+'</span><span style="display:inline-flex;align-items:center;gap:6px" class="num" style="color:'+o.color+'"><span class=dot style="background:'+o.dot+'"></span>'+o.val+'</span></div>').join('');

 buildCharts(s,B);
 const p=document.getElementById('pulse');p.style.opacity='.3';setTimeout(()=>p.style.opacity='1',300);
}

function buildCharts(s,B){
 const dates=slice(B.s4.dates,rangeN());
 function align(book){const map=new Map();book.dates.forEach((d,i)=>map.set(d,book.eq[i]));return dates.map(d=>map.get(d)??null);}
 const bm=daily(s.benchmark);
 function alignBm(){const map=new Map();bm.d.forEach((d,i)=>map.set(d,bm.v[i]));return dates.map(d=>map.get(d)??null);}
 function toMode(vals){if(MODE==='usd')return vals;const base=vals.find(x=>x!=null);if(base==null)return vals;return vals.map(x=>x==null?null:(x/base-1)*100);}
 const ds=Object.keys(B).map(k=>({label:NM[k],data:toMode(align(B[k])),borderColor:COL[k],backgroundColor:'transparent',borderWidth:k==='s4'?2.4:(k==='s5'?1.8:1.3),borderDash:k==='s5'?[2,2]:undefined,pointRadius:0,tension:.25,spanGaps:true}));
 ds.push({label:'SPY',data:toMode(alignBm()),borderColor:'#9aa5b1',borderDash:[5,4],backgroundColor:'transparent',borderWidth:1.3,pointRadius:0,tension:.25,spanGaps:true});
 const yFmt=MODE==='usd'?(v=>'$'+(v/1e6).toFixed(2)+'M'):(v=>v.toFixed(2)+'%');
 const axis={grid:{color:'#eef1f5'},ticks:{color:'#94a3b8',font:{family:'IBM Plex Mono',size:10}},border:{color:'#e3e8ef'}};
 const cfg={type:'line',data:{labels:dates.map(d=>d.slice(5)),datasets:ds},options:{responsive:true,maintainAspectRatio:false,animation:false,
  interaction:{mode:'index',intersect:false},
  plugins:{legend:{display:false},tooltip:{backgroundColor:'#fff',borderColor:'#e3e8ef',borderWidth:1,titleColor:'#64748b',bodyColor:'#1a2233',bodyFont:{family:'IBM Plex Mono'},
   callbacks:{label:c=>c.dataset.label+'  '+(c.parsed.y==null?'—':(MODE==='usd'?'$'+Math.round(c.parsed.y).toLocaleString():c.parsed.y.toFixed(2)+'%'))}}},
  scales:{y:Object.assign({},axis,{ticks:Object.assign({},axis.ticks,{callback:yFmt})}),
   x:Object.assign({},axis,{grid:{display:false},ticks:Object.assign({},axis.ticks,{maxTicksLimit:9})})}}};
 if(eqChart){eqChart.data=cfg.data;eqChart.options=cfg.options;eqChart.update('none');}else eqChart=new Chart(document.getElementById('eq'),cfg);

 const ddv=ddSeries(slice(B.s4.eq,rangeN()));
 const ddCfg={type:'line',data:{labels:dates.map(d=>d.slice(5)),datasets:[
  {data:ddv,borderColor:gneg,backgroundColor:'rgba(185,28,28,.07)',fill:true,borderWidth:1.4,pointRadius:0,tension:.2},
  {data:dates.map(()=>-10),borderColor:'#d97706',borderDash:[4,4],borderWidth:1,pointRadius:0},
  {data:dates.map(()=>-15),borderColor:gneg,borderDash:[4,4],borderWidth:1,pointRadius:0}]},
  options:{responsive:true,maintainAspectRatio:false,animation:false,plugins:{legend:{display:false},tooltip:{enabled:false}},
   scales:{y:Object.assign({},axis,{min:-16,max:1,ticks:Object.assign({},axis.ticks,{callback:v=>v+'%'})}),x:{display:false}}}};
 if(ddChart){ddChart.data=ddCfg.data;ddChart.update('none');}else ddChart=new Chart(document.getElementById('dd'),ddCfg);
}

function openBook(k){
 ACTIVE_BOOK=k;
 const s=LAST;const B=bookStats(s,k);
 document.getElementById('shSwatch').style.background=COL[k];
 document.getElementById('shName').textContent=NM[k];
 document.getElementById('shDesc').textContent=DESC[k];
 const stats=[
  {lbl:'Equity',val:fmt$(B.equity),color:navy},
  {lbl:'Return',val:pct(B.ret),color:B.ret>=0?gpos:gneg},
  {lbl:'Ann. vol',val:B.vol==null?'—':(B.vol*100).toFixed(1)+'%',color:navy},
  {lbl:'Sharpe',val:B.sharpe==null?'—':B.sharpe.toFixed(2),color:navy},
  {lbl:'Max DD',val:(B.dd*100).toFixed(1)+'%',color:gneg},
  {lbl:'Gross / names',val:Math.round(gross(B.weights)*100)+'% / '+Object.keys(B.weights).length,color:navy},
 ];
 document.getElementById('shStats').innerHTML=stats.map(st=>
  '<div class=statcell><div class=lbl>'+st.lbl+'</div><div class="num" style="font-size:16px;font-weight:600;margin-top:3px;color:'+st.color+'">'+st.val+'</div></div>').join('');

 const ents=Object.entries(B.weights).sort((a,b)=>Math.abs(b[1])-Math.abs(a[1]));
 const posHdr=document.getElementById('shPosHdr');
 posHdr.innerHTML='Positions <span class=muted style="text-transform:none">— '+ents.length+' total'
  +(ents.length>MAX_POS_ROWS?', showing top '+MAX_POS_ROWS+' by |weight|':'')+'</span>';
 const posEl=document.getElementById('shPositions');
 if(!ents.length){
  posEl.innerHTML='<div class=sec style="font-size:12px;padding:14px;background:#fafbfc;border:1px solid #e3e8ef;border-radius:8px;line-height:1.5">'
   +NM[k]+' currently holds no positions (warming up or fully flat).</div>';
 } else {
  const shown=ents.slice(0,MAX_POS_ROWS).sort((a,b)=>b[1]-a[1]);
  posEl.innerHTML=shown.map(([sym,w])=>{
   const pos=w>=0,color=pos?gpos:gneg,width=Math.min(Math.abs(w)/0.05,1)*48,left=pos?50:(50-width);
   return '<div class=posrow><span class="sym num" style="width:70px">'+sym+'</span>'
    +'<div class="bar" style="height:12px;position:relative;background:#f1f4f8"><div style="position:absolute;top:0;bottom:0;width:1px;background:#cbd5e1;left:50%"></div>'
    +'<div style="position:absolute;top:2px;bottom:2px;border-radius:2px;background:'+color+';left:'+left.toFixed(1)+'%;width:'+width.toFixed(1)+'%"></div></div>'
    +'<span class="wpct num" style="color:'+color+'">'+(w*100).toFixed(1)+'%</span></div>';
  }).join('');
 }

 document.getElementById('sheet').classList.add('open');
 document.getElementById('sheetBackdrop').classList.add('open');
 setTimeout(()=>{
  const cfg={type:'line',data:{labels:B.dates.map(d=>d.slice(5)),datasets:[{data:B.eq,borderColor:COL[k],backgroundColor:COL[k]+'14',fill:true,borderWidth:1.8,pointRadius:0,tension:.25}]},
   options:{responsive:true,maintainAspectRatio:false,animation:false,plugins:{legend:{display:false},tooltip:{enabled:false}},
    scales:{y:{grid:{color:'#eef1f5'},ticks:{color:'#94a3b8',font:{family:'IBM Plex Mono',size:9},callback:v=>'$'+(v/1e6).toFixed(3)+'M'}},x:{display:false}}}};
  if(sparkChart)sparkChart.destroy();
  sparkChart=new Chart(document.getElementById('spark'),cfg);
 },30);
}
function closeBook(){ACTIVE_BOOK=null;document.getElementById('sheet').classList.remove('open');document.getElementById('sheetBackdrop').classList.remove('open');}
document.getElementById('shClose').onclick=closeBook;
document.getElementById('sheetBackdrop').onclick=closeBook;

async function load(){
 try{LAST=await(await fetch('/api/state',{cache:'no-store'})).json();}catch(e){return;}
 render(LAST);
}
function setToggle(groupIds,onId){groupIds.forEach(id=>document.getElementById(id).classList.toggle('on',id===onId));}
document.getElementById('mUsd').onclick=()=>{MODE='usd';setToggle(['mUsd','mPct'],'mUsd');if(LAST)buildCharts(LAST,statsMap(LAST));};
document.getElementById('mPct').onclick=()=>{MODE='pct';setToggle(['mUsd','mPct'],'mPct');if(LAST)buildCharts(LAST,statsMap(LAST));};
document.getElementById('r7').onclick=()=>{RANGE='7';setToggle(['r7','r30','rAll'],'r7');if(LAST)buildCharts(LAST,statsMap(LAST));};
document.getElementById('r30').onclick=()=>{RANGE='30';setToggle(['r7','r30','rAll'],'r30');if(LAST)buildCharts(LAST,statsMap(LAST));};
document.getElementById('rAll').onclick=()=>{RANGE='all';setToggle(['r7','r30','rAll'],'rAll');if(LAST)buildCharts(LAST,statsMap(LAST));};
function clk(){document.getElementById('clk').textContent=new Date().toISOString().slice(11,19)+' UTC';}
clk();setInterval(clk,1000);load();setInterval(load,60000);
</script></body></html>"""


def make_handler(state_path: Path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/state"):
                data = state_path.read_bytes() if state_path.exists() else b"{}"
                self._send(data, "application/json")
            else:
                self._send(_HTML.encode(), "text/html; charset=utf-8")

    return H


def start_dashboard(state_path: str = "data/state.json", port: int = 8080,
                    host: str = "0.0.0.0") -> ThreadingHTTPServer:
    # Bound publicly on :8080 (user request). No auth - treat as paper-only.
    server = ThreadingHTTPServer((host, port), make_handler(Path(state_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"dashboard on http://{host}:{port}")
    return server
