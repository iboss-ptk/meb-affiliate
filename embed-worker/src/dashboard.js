// Public crawl dashboard: reads /status (counts only) every 30 s.
export const DASHBOARD = `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>mebreco crawl</title>
<style>
  :root{--bg:#faf7f2;--ink:#1f1b16;--muted:#75695c;--line:#e6ddd0;--accent:#c2410c;--ok:#15803d;--warn:#b45309}
  *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}
  main{max-width:860px;margin:0 auto;padding:24px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 20px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
  .card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px}
  .card h2{font-size:13px;font-weight:600;color:var(--muted);margin:0 0 6px;text-transform:uppercase;letter-spacing:.04em}
  .big{font-size:28px;font-weight:700}.small{font-size:13px;color:var(--muted)}
  .track{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin:8px 0 4px}
  .fill{height:100%;background:var(--accent);transition:width .6s}
  .pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:13px;font-weight:600;background:var(--line)}
  .running{background:#dcfce7;color:var(--ok)}.blocked{background:#fee2e2;color:#b91c1c}.idle{background:var(--line);color:var(--muted)}
  .banner{background:#fee2e2;color:#7f1d1d;border-radius:10px;padding:10px 14px;margin-bottom:14px}
  a{color:var(--accent)}
</style></head><body><main>
<h1>MEB catalog crawl</h1><p class="sub" id="sub">loading…</p>
<div id="banner"></div><div class="grid" id="grid"></div>
</main><script>
const fmt=n=>Number(n||0).toLocaleString();
const ago=t=>{const s=Math.round(Date.now()/1000-t);return s<90?s+" s ago":s<5400?Math.round(s/60)+" min ago":(s/3600).toFixed(1)+" h ago"};
const bar=(n,d)=>{const p=d?Math.min(100,100*n/d):0;return '<div class="track"><div class="fill" style="width:'+p.toFixed(1)+'%"></div></div><div class="small">'+p.toFixed(1)+'%</div>'};
async function load(){
  let s; try{const r=await fetch("/status",{cache:"no-store"}); if(r.status===404){document.getElementById("sub").textContent="No run has reported yet.";return} s=await r.json()}catch(e){return}
  const run=s.run||{}, live=!run.result && Date.now()/1000-s.updated<900;
  const state=live?'<span class="pill running">running · '+(run.stage||"")+'</span>':'<span class="pill idle">'+(run.result||"idle")+'</span>';
  document.getElementById("sub").innerHTML=state+' &nbsp; updated '+ago(s.updated)+(run.url?' · <a href="'+run.url+'" target="_blank" rel="noopener">GitHub run</a>':'');
  document.getElementById("banner").innerHTML=s.blocked_until?'<div class="banner">MEB blocked the crawler. Holding off until '+new Date(s.blocked_until*1000).toLocaleString()+'.</div>':'';
  const fv=s.first_volumes||{}, ne=s.neurons_today||{}, c=s.categories||{};
  document.getElementById("grid").innerHTML=[
    ['Categories listed', fmt(c.done)+' / '+fmt(c.total), bar(c.done,c.total)],
    ['Books found', fmt(s.listings), '<div class="small">'+fmt(s.listing_pages)+' listing pages · '+fmt(s.sets)+' SET bundles skipped</div>'],
    ['First volumes crawled', fmt(fv.crawled)+' / '+fmt(fv.total), bar(fv.crawled,fv.total)],
    ['Embedded', fmt(s.embedded)+' / '+fmt(s.books), bar(s.embedded,s.books)],
    ['Workers AI today', fmt(ne.used)+' / '+fmt(ne.limit)+' neurons', bar(ne.used,ne.limit)],
  ].map(([h,b,x])=>'<div class="card"><h2>'+h+'</h2><div class="big">'+b+'</div>'+x+'</div>').join("");
}
load(); setInterval(load,30000);
</script></body></html>`;
