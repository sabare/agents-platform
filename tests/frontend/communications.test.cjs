const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/agents_platform/static/app.js','utf8');
const escape = s=>String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
function render(name,end,state,task){
  const context={state,e:escape,closed:()=>false,statusName:s=>s,date:s=>s,localInput:()=>"",Intl};
  const start=source.indexOf(`function ${name}(`);
  vm.runInNewContext(source.slice(start,source.indexOf(end,start)),context);
  return context[name](task);
}
test('simulated call form uses case contacts and preserves an escaped transcript draft',()=>{
  const html=render('communicationHTML','document.addEventListener("input",event=>{',{
    overview:{contacts:[{id:'one',case_id:'case',name:'Hospital'},{id:'two',case_id:'other',name:'Wrong case'}]},
    communicationDraft:{task:'task',transcript:'<script>bad</script>'}
  },{id:'task',kind:'AGENT',case_id:'case',task_type:'medical_records'});
  assert.match(html,/No phone call is placed/);
  assert.match(html,/Hospital/);
  assert.doesNotMatch(html,/Wrong case|<script>/);
  assert.match(html,/&lt;script>/);
});
test('communication authorization review requires a document and both attestations',()=>{
  const html=render('humanRequestsHTML','function localInput(',{
    humanRequests:[{id:'review',kind:'REVIEW',status:'OPEN',version:1,actionable:true,
      allowed_actions:[{action:'RESOLVE_COMMUNICATION',label:'Resolve',payload_schema:{required:['authorization_source_id']}}],
      decisions:[],continuations:[],evidence_source_ids:['source']}]
  },{id:'task',version:2});
  assert.match(html,/name="authorization_file"[^>]*required/);
  assert.match(html,/name="confirmed_reviewed"[^>]*required/);
  assert.match(html,/name="restrictions_resolved"[^>]*required/);
  assert.doesNotMatch(html,/name="authorization_response_due_at"[^>]*required/);
});

test('current review precedes collapsed history and technical assessment stays expandable',()=>{
  const request=(id,status)=>({id,kind:'REVIEW',status,version:1,actionable:false,
    question:id,context_summary:'Long technical assessment',allowed_actions:[],
    decisions:[],continuations:[],evidence_source_ids:[]});
  const html=render('humanRequestsHTML','function localInput(',{
    humanRequests:[request('old-review','SUPERSEDED'),request('current-review','OPEN')]
  },{id:'task',version:2});
  assert.ok(html.indexOf('current-review') < html.indexOf('old-review'));
  assert.match(html,/<details class="task-memory review-history"><summary>Previous reviews \(1\)/);
  assert.match(html,/<details class="review-assessment"><summary>Agent assessment/);
});

test('incoming update remains visibly queued until its source-linked interpretation arrives',()=>{
  const start=source.indexOf('function pendingUpdateHTML()');
  const code=source.slice(start,source.indexOf('function renderDetail()',start));
  const state={events:[{type:'provider.response_ingested',payload:{source_id:'raw'}}]};
  const context={state};vm.runInNewContext(code,context);
  assert.match(context.pendingUpdateHTML(),/Update received/);
  state.events.push({type:'provider.response_extracted',payload:{raw_source_id:'raw'}});
  assert.equal(context.pendingUpdateHTML(),'');
});

test('authorization review restores the deadline and attestation after refresh',()=>{
  const html=render('humanRequestsHTML','function localInput(',{
    humanDrafts:{review:{authorization_response_due_at:'2026-11-20T09:00',confirmed_valid:'on'}},
    humanRequests:[{id:'review',kind:'REVIEW',status:'OPEN',version:1,actionable:true,
      allowed_actions:[{action:'PROVIDE_UPDATED_AUTHORIZATION',label:'Provide'}],
      decisions:[],continuations:[],evidence_source_ids:[]}]
  },{id:'task',version:2});
  assert.match(html,/name="authorization_response_due_at"[^>]*value="2026-11-20T09:00"/);
  assert.match(html,/name="confirmed_valid"[^>]*checked/);
});
