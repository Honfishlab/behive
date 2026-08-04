const questions=[
 {title:'How will compute supply change?',confidence:82,findings:7,kind:'COMPUTE'},
 {title:'Where will power become the binding constraint?',confidence:54,findings:3,kind:'ENERGY'},
 {title:'How will inference pricing affect demand?',confidence:63,findings:5,kind:'ECONOMICS'},
 {title:'Which regulations alter deployment economics?',confidence:41,findings:2,kind:'REGULATION'}];
const findings=[
 {time:'12 MIN AGO',score:89,text:'Power availability, rather than chip supply, is becoming the primary constraint for several planned data-center regions.',source:'IEA electricity outlook · 3 supporting sources'},
 {time:'38 MIN AGO',score:81,text:'Inference unit costs continue to decline, but increased reasoning-time compute may offset efficiency gains.',source:'Provider pricing records · 4 supporting sources'},
 {time:'2 HOURS AGO',score:74,text:'Advanced packaging capacity remains concentrated across a small supplier set through the forecast period.',source:'Supplier filings · 2 supporting sources'},
 {time:'YESTERDAY',score:62,text:'Permitting timelines vary enough by region to materially change data-center project economics.',source:'Regulatory review · needs verification'}];
const agents=[['PL','Planner','Ranking research frontier','ACTIVE'],['SC','Scout','Searching energy sources','ACTIVE'],['VR','Verifier','Checking 3 claims','ACTIVE'],['CN','Connector','Mapping cross-thread links','IDLE']];
function render(){
 document.querySelector('#questionTree').innerHTML=questions.map((q,i)=>`<div class="branch"><span class="node"></span><div class="question-card"><span class="tag">${q.kind}</span><h3>${q.title}</h3><div class="bar"><i style="width:${q.confidence}%"></i></div><small>${q.confidence}% confidence · ${q.findings} findings</small></div></div>`).join('');
 document.querySelector('#findingFeed').innerHTML=findings.map(f=>`<article class="finding"><div class="meta"><span>${f.time}</span><span class="confidence">${f.score}% confidence</span></div><p>${f.text}</p><a href="#">${f.source} →</a></article>`).join('');
 document.querySelector('#agents').innerHTML=agents.map(a=>`<div class="agent"><span class="avatar">${a[0]}</span><div><b>${a[1]}</b><small>${a[2]}</small></div><span>${a[3]}</span></div>`).join('');
 drawGraph();
}
function drawGraph(){const svg=document.querySelector('#graphSvg');let nodes=[{x:450,y:70,t:'How will AI infrastructure evolve?',c:'#215b43'}];questions.forEach((q,i)=>nodes.push({x:150+i*200,y:230,t:q.kind,c:'#6989bd'}));findings.slice(0,4).forEach((f,i)=>nodes.push({x:150+i*200,y:410,t:f.score+'% finding',c:'#d99b35'}));let lines=questions.map((_,i)=>`<line x1="450" y1="70" x2="${150+i*200}" y2="230"/>`).join('')+questions.map((_,i)=>`<line x1="${150+i*200}" y1="230" x2="${150+i*200}" y2="410"/>`).join('');svg.innerHTML=`<g stroke="#c7d0c8" stroke-width="2">${lines}</g>`+nodes.map(n=>`<g><circle cx="${n.x}" cy="${n.y}" r="18" fill="${n.c}"/><text x="${n.x}" y="${n.y+34}" text-anchor="middle" font-size="11" fill="#34443b">${n.t}</text></g>`).join('')}
document.querySelectorAll('[data-panel]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-panel]').forEach(x=>x.classList.remove('active'));b.classList.add('active');let graph=b.dataset.panel==='graph';document.querySelector('#workspace').classList.toggle('hidden',graph);document.querySelector('#graphView').classList.toggle('hidden',!graph)});
const toggle=document.querySelector('#toggleRun');toggle.onclick=()=>{let running=toggle.textContent==='Start research';toggle.textContent=running?'Pause research':'Start research';document.querySelector('#runStatus').textContent=running?'4 agents researching':'Agents paused'};
['budget','maxAgents'].forEach(id=>document.querySelector('#'+id).oninput=e=>document.querySelector('#'+id.replace('maxAgents','agents')+'Out').textContent=e.target.value);
document.querySelector('#autonomy').onchange=e=>document.querySelector('#autonomyBadge').textContent=e.target.value.toUpperCase();
const dlg=document.querySelector('#questionDialog');document.querySelector('#addQuestion').onclick=()=>dlg.showModal();dlg.addEventListener('close',()=>{let text=dlg.querySelector('textarea').value.trim();if(dlg.returnValue==='add'&&text){questions.push({title:text,confidence:0,findings:0,kind:'NEW LEAD'});dlg.querySelector('textarea').value='';render()}});
document.querySelector('#savePolicy').onclick=e=>{e.target.textContent='Saved ✓';setTimeout(()=>e.target.textContent='Save policy',1300)};
render();
