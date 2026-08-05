let projectId=localStorage.getItem('behiveProjectId');
let questions=[];
let rootNode=null;
let selectedQuestion=null;
let dialogMode='add';
const findings=[
 {time:'12 MIN AGO',score:89,text:'Power availability is becoming a primary constraint for planned data-center regions.',source:'Example finding · start research to replace'},
 {time:'38 MIN AGO',score:81,text:'Inference unit costs continue to decline while reasoning-time compute increases.',source:'Example finding · start research to replace'}];
const agents=[['PL','Planner','Ranking research frontier','ACTIVE'],['SC','Scout','Searching sources','ACTIVE'],['VR','Verifier','Checking claims','ACTIVE'],['CN','Connector','Mapping cross-thread links','IDLE']];
const $=selector=>document.querySelector(selector);
function escapeHtml(value){const div=document.createElement('div');div.textContent=value??'';return div.innerHTML}
async function api(path,options={}){const response=await fetch(path,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.detail||data.message||`Request failed (${response.status})`);return data}
async function ensureProject(){
 if(projectId){
  try{await loadTree();return}
  catch(error){localStorage.removeItem('behiveProjectId');projectId=null;rootNode=null;selectedQuestion=null}
 }
 try{
  const created=await api('/projects',{method:'POST',body:JSON.stringify({title:$('#projectTitle').textContent.trim(),root_question:$('#rootQuestion').textContent.trim(),cadence_minutes:1440})});
  projectId=created.id;localStorage.setItem('behiveProjectId',projectId);await loadTree();
 }catch(error){$('#missionMessage').textContent=`Project storage unavailable: ${error.message}`}
}
async function loadTree(){
 const data=await api(`/projects/${projectId}/map`);const nodes=data.nodes||[];
 rootNode=nodes.find(n=>n.type==='question'&&!n.parent_id)||null;
 questions=nodes.filter(n=>n.type==='question'&&n.parent_id&&n.status!=='archived').map(n=>({id:n.id,parentId:n.parent_id,title:n.label,depth:n.depth,confidence:Math.round((n.confidence||0)*100),findings:0,kind:(n.kind||'followup').toUpperCase(),priority:n.priority,status:n.status}));
 if(rootNode){$('#rootQuestion').textContent=rootNode.label;$('#rootTreeTitle').textContent=rootNode.label;$('#rootTreeMeta').textContent=`${Math.round((rootNode.confidence||0)*100)}% confidence · priority ${Math.round((rootNode.priority||1)*100)}%`}
 render();
}
function render(){
 $('#questionTree').innerHTML=questions.sort((a,b)=>a.depth-b.depth||b.priority-a.priority).map(q=>`<div class="branch" style="margin-left:${Math.max(0,q.depth-1)*24}px"><span class="node"></span><div class="question-card ${selectedQuestion?.id===q.id?'selected':''}" data-id="${q.id}"><span class="tag">${escapeHtml(q.kind)} · ${escapeHtml(q.status)}</span><h3>${escapeHtml(q.title)}</h3><div class="bar"><i style="width:${q.confidence}%"></i></div><small>${q.confidence}% confidence · priority ${Math.round(q.priority*100)}%</small></div></div>`).join('')||'<p class="sub">Select Add question to create the first branch.</p>';
 $('#findingFeed').innerHTML=findings.map(f=>`<article class="finding"><div class="meta"><span>${escapeHtml(f.time)}</span><span class="confidence">${f.score}% confidence</span></div><p>${escapeHtml(f.text)}</p><a href="${f.source?.startsWith('http')?escapeHtml(f.source):'#'}">${escapeHtml(f.source)} →</a></article>`).join('');
 $('#agents').innerHTML=agents.map(a=>`<div class="agent"><span class="avatar">${a[0]}</span><div><b>${a[1]}</b><small>${a[2]}</small></div><span>${a[3]}</span></div>`).join('');
 document.querySelectorAll('.question-card').forEach(card=>{card.onclick=()=>selectQuestion(card.dataset.id);card.ondblclick=()=>openEdit()});
 $('#editQuestion').disabled=!selectedQuestion;drawGraph();
}
function selectQuestion(id){selectedQuestion=questions.find(q=>q.id===id)||null;$('#rootTreeCard').classList.remove('selected');render();$('#addQuestion').textContent=selectedQuestion?'＋ Add child':'＋ Add question'}
async function selectRoot(){if(!rootNode)await ensureProject();if(!rootNode){$('#missionMessage').textContent='The root question could not be loaded. Check database health and refresh.';return}selectedQuestion={id:rootNode.id,title:rootNode.label,depth:0,confidence:Math.round((rootNode.confidence||0)*100),findings:0,kind:'ROOT',priority:rootNode.priority||1,status:rootNode.status||'open',isRoot:true};$('#rootTreeCard').classList.add('selected');render();$('#rootTreeCard').classList.add('selected');$('#addQuestion').textContent='＋ Add child'}
function drawGraph(){const svg=$('#graphSvg');const shown=questions.slice(0,4);let nodes=[{x:450,y:70,t:'Root question',c:'#215b43'}];shown.forEach((q,i)=>nodes.push({x:150+i*200,y:230,t:q.kind,c:'#6989bd'}));findings.slice(0,shown.length).forEach((f,i)=>nodes.push({x:150+i*200,y:410,t:f.score+'% finding',c:'#d99b35'}));let lines=shown.map((_,i)=>`<line x1="450" y1="70" x2="${150+i*200}" y2="230"/>`).join('')+findings.slice(0,shown.length).map((_,i)=>`<line x1="${150+i*200}" y1="230" x2="${150+i*200}" y2="410"/>`).join('');svg.innerHTML=`<g stroke="#c7d0c8" stroke-width="2">${lines}</g>`+nodes.map(n=>`<g><circle cx="${n.x}" cy="${n.y}" r="18" fill="${n.c}"/><text x="${n.x}" y="${n.y+34}" text-anchor="middle" font-size="11" fill="#34443b">${escapeHtml(n.t)}</text></g>`).join('')}
const dlg=$('#questionDialog');
function openAdd(){dialogMode='add';$('#questionDialogTitle').textContent=selectedQuestion?'Add child question':'Add research question';$('#questionText').value='';$('#questionPriority').value='0.5';$('#questionStatus').value='open';$('#questionParent').textContent=selectedQuestion?`Parent: ${selectedQuestion.title}`:'New top-level branch';$('#archiveQuestion').hidden=true;dlg.showModal()}
function openEdit(){if(!selectedQuestion)return;dialogMode='edit';$('#questionDialogTitle').textContent=selectedQuestion.isRoot?'Edit root research question':'Edit research question';$('#questionText').value=selectedQuestion.title;$('#questionPriority').value=selectedQuestion.priority;$('#questionStatus').value=selectedQuestion.status;$('#questionParent').textContent=selectedQuestion.isRoot?'Root of this investigation':`Depth ${selectedQuestion.depth}`;$('#archiveQuestion').hidden=Boolean(selectedQuestion.isRoot);dlg.showModal()}
$('#addQuestion').onclick=openAdd;$('#editQuestion').onclick=openEdit;
$('#rootTreeCard').onclick=()=>selectRoot();$('#rootTreeCard').ondblclick=async()=>{await selectRoot();if(selectedQuestion?.isRoot)openEdit()};
dlg.addEventListener('close',async()=>{
 if(!['save','archive'].includes(dlg.returnValue))return;
 try{
  if(dlg.returnValue==='archive'){await api(`/projects/${projectId}/questions/${selectedQuestion.id}`,{method:'PATCH',body:JSON.stringify({status:'archived'})});selectedQuestion=null}
  else{const text=$('#questionText').value.trim();if(text.length<8)throw new Error('Question must be at least 8 characters');const payload={question:text,priority:Number($('#questionPriority').value),status:$('#questionStatus').value};if(dialogMode==='add'){payload.parent_id=selectedQuestion?.id||rootNode?.id;await api(`/projects/${projectId}/questions`,{method:'POST',body:JSON.stringify(payload)})}else await api(`/projects/${projectId}/questions/${selectedQuestion.id}`,{method:'PATCH',body:JSON.stringify(payload)})}
  await loadTree();$('#missionMessage').textContent='Question tree saved.';
 }catch(error){$('#missionMessage').textContent=`Could not save question: ${error.message}`}
});
$('#rootQuestion').addEventListener('blur',async()=>{if(!rootNode)return;const question=$('#rootQuestion').textContent.trim();if(question.length<8)return;try{await api(`/projects/${projectId}/questions/${rootNode.id}`,{method:'PATCH',body:JSON.stringify({question})});rootNode.label=question;$('#missionMessage').textContent='Root question saved.'}catch(error){$('#missionMessage').textContent=error.message}});
document.querySelectorAll('[data-panel]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-panel]').forEach(x=>x.classList.remove('active'));b.classList.add('active');let graph=b.dataset.panel==='graph';$('#workspace').classList.toggle('hidden',graph);$('#graphView').classList.toggle('hidden',!graph)});
const toggle=$('#toggleRun'),runStatus=$('#runStatus'),missionMessage=$('#missionMessage');let activeMission=null,missionTimer=null;
async function pollMission(){if(!activeMission)return;try{const data=await api(`/research/${activeMission}/status`);const count=data.claims_count??data.total_claims??0;runStatus.textContent=`${data.phase||data.status} · ${count} findings`;missionMessage.textContent=`Mission ${activeMission} · ${data.status} · ${data.phase||'starting'}`;if(['done','error','cancelled'].includes(data.status)){clearInterval(missionTimer);missionTimer=null;toggle.disabled=false;toggle.textContent='Start another';if(data.status==='done')await loadMissionResults()}}catch(error){runStatus.textContent='Status unavailable';missionMessage.textContent=error.message}}
async function loadMissionResults(){try{const data=await api(`/research/${activeMission}`);const claims=data.claims||[];findings.splice(0,findings.length,...claims.slice(0,20).map(c=>({time:'NEW FINDING',score:Math.round((c.confidence||c.quality_score||0)*100),text:c.text||c.claim,source:c.source_url||'Source pending'})));render();missionMessage.textContent=`Complete · ${claims.length} sourced findings available`}catch(error){missionMessage.textContent=`Research complete; results unavailable: ${error.message}`}}
toggle.onclick=async()=>{const question=$('#rootQuestion').textContent.trim();if(question.length<8){missionMessage.textContent='Enter a research question of at least 8 characters.';return}toggle.disabled=true;toggle.textContent='Starting…';runStatus.textContent='Starting research';try{await ensureProject();const data=await api('/research',{method:'POST',body:JSON.stringify({query:question,depth:3,scale:30})});activeMission=data.job_id||data.mission_id;toggle.textContent='Research running';await pollMission();missionTimer=setInterval(pollMission,3000)}catch(error){toggle.disabled=false;toggle.textContent='Start research';runStatus.textContent='Could not start';missionMessage.textContent=error.message}};
['budget','maxAgents'].forEach(id=>$('#'+id).oninput=e=>$('#'+id.replace('maxAgents','agents')+'Out').textContent=e.target.value);$('#autonomy').onchange=e=>$('#autonomyBadge').textContent=e.target.value.toUpperCase();$('#savePolicy').onclick=async e=>{try{await ensureProject();await api(`/projects/${projectId}/policy`,{method:'PUT',body:JSON.stringify({autonomy:$('#autonomy').value,status:'paused',daily_budget:Number($('#budget').value),max_agents:Number($('#maxAgents').value),max_depth:5,min_confidence:.7,primary_sources_required:true,approval_new_branches:true,freshness_days:30})});e.target.textContent='Saved ✓'}catch(error){missionMessage.textContent=error.message}setTimeout(()=>e.target.textContent='Save policy',1300)};
render();ensureProject();
