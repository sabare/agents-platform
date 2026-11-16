const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../src/agents_platform/static/app.js'), 'utf8');

// Execute the actual form listeners with a minimal DOM boundary. No browser or service needed.
function harness(intervention, automatic=false) {
  const listeners = {};
  const fields = new Map();
  const field = (selector) => {
    if (!fields.has(selector)) fields.set(selector, {value:'',checked:false,disabled:false,hidden:false,close(){}});
    return fields.get(selector);
  };
  field('#create-agent').value = 'supervisor';
  field('#create-scope').checked = true;
  field('#create-replace').checked = true;
  field('#all-dates').checked = true;
  field('#create-case').value = 'case-1';
  field('#create-patient').value = 'patient-1';
  field('#create-provider').value = 'provider-1';
  field('#create-objective').value = 'Collect the corrected records scope';
  const state = {firm:'firm-1',createKey:'old-key',createAt:'old-time',
    createSupervisor:{revision:2},humanObjective:intervention};
  field('#create-agent').disabled = field('#create-case').disabled = Boolean(intervention);
  field('#create-form').addEventListener = (event, callback) => listeners[event] = callback;
  const requests=[];
  const context = {state,$:field,$$:() => [{value:'RECORDS'}],isMonthlyCreate:()=>false,
    automaticCreate:()=>automatic,invalidateAssignmentPlan(){},assignmentPlanHTML:()=>"plan preview",
    crypto:{randomUUID:()=> 'new-key'},toast(){},refresh:async()=>{},openTask:async()=>{},
    api:async(url,options) => {requests.push({url,body:JSON.parse(options.body)});return {group_task_id:'group-1'};}};
  const start = source.indexOf('$("#create-form").addEventListener("submit",');
  const end = source.indexOf('$("#comment-form").addEventListener("submit",',start);
  assert.ok(start >= 0 && end > start);
  vm.runInNewContext(source.slice(start,end),context);
  return {state,listeners,field,requests};
}

test('editing a supervisor clarification preserves the review and submits an audited decision',async()=>{
  const review={request:{id:'review-1',case_id:'case-1',task_id:'group-1',task_version:4,version:2},reason:'Clarified requested provider'};
  const h=harness(review);
  h.listeners.input();
  assert.equal(h.state.humanObjective,review);
  assert.equal(h.field('#create-case').disabled,true);
  assert.equal(h.field('#create-agent').disabled,true);
  assert.equal(h.state.createKey,null);
  const button={disabled:false};
  await h.listeners.submit({preventDefault(){},submitter:button});
  assert.equal(h.requests.length,1);
  assert.match(h.requests[0].url,/human-requests\/review-1\/decisions$/);
  assert.equal(h.requests[0].body.action,'REVISE_OBJECTIVE');
  assert.equal(h.requests[0].body.reason,review.reason);
  assert.equal(h.requests[0].body.payload.objective,'Collect the corrected records scope');
  assert.equal(button.disabled,false);
});

test('ordinary assignment edits still use the objective endpoint',async()=>{
  const h=harness(null);
  h.listeners.input();
  assert.equal(h.field('#create-case').disabled,false);
  await h.listeners.submit({preventDefault(){},submitter:{disabled:false}});
  assert.equal(h.requests.length,1);
  assert.match(h.requests[0].url,/cases\/case-1\/supervisor$/);
});

for (const [choice, agent, mode] of [
  ['records', 'supervisor', 'records'],
  ['monthly', 'supervisor', 'monthly'],
  ['check_in', 'check_in', 'records'],
  ['medical_records', 'medical_records', 'records'],
]) {
  test(`simple workflow choice ${choice} selects the correct creation path`, () => {
    const fields = new Map();
    let change;
    let dispatched = 0;
    const $ = selector => {
      if (!fields.has(selector)) fields.set(selector, {value:'', addEventListener:(_, cb) => change=cb,
        dispatchEvent:event => { assert.equal(event.type,'change'); dispatched++; }});
      return fields.get(selector);
    };
    const start=source.indexOf('$("#create-workflow").addEventListener("change",');
    const end=source.indexOf('function updateCreateKind()',start);
    vm.runInNewContext(source.slice(start,end), {$,state:{humanObjective:null},Event:class {constructor(type){this.type=type;}}});
    $('#create-workflow').value=choice;
    $('#create-objective').value='Previous workflow objective';
    change();
    assert.equal($('#create-agent').value,agent);
    assert.equal($('#create-supervisor-mode').value,mode);
    assert.equal($('#create-objective').value,'');
    assert.equal(dispatched,1);
  });
}

test('workflow selection cannot redirect an active human clarification', () => {
  let change;
  let refreshed=false;
  const $ = selector => {
    assert.equal(selector,'#create-workflow');
    return {addEventListener:(_,cb) => change=cb};
  };
  const start=source.indexOf('$("#create-workflow").addEventListener("change",');
  const end=source.indexOf('function updateCreateKind()',start);
  vm.runInNewContext(source.slice(start,end),{$,state:{humanObjective:{}},updateCreateKind:()=>refreshed=true});
  change();
  assert.equal(refreshed,true);
});


test('description-first submit requests a plan without creating an assignment',async()=>{
  const h=harness(null,true);
  await h.listeners.submit({preventDefault(){},submitter:{disabled:false}});
  assert.equal(h.requests.length,1);
  assert.match(h.requests[0].url,/assignments\/plan$/);
  assert.equal(h.requests[0].body.description,'Collect the corrected records scope');
  assert.equal(h.field('#create-plan').hidden,false);
});

function contextForm() {
  const fields=new Map();
  const $=selector=>{
    if (!fields.has(selector)) fields.set(selector,{value:'',checked:false,disabled:false,dataset:{}});
    return fields.get(selector);
  };
  const manual=[$('#create-patient'),$('#create-provider'),$('#monthly-zone'),$('#create-agent'),$('#create-workflow')];
  $('#create-agent').value='supervisor';
  $('#create-supervisor-mode').value='records';
  $('#create-scope').checked=true;
  $('#all-dates').checked=true;
  const state={humanObjective:null,createSupervisor:{revision:0}};
  const $$=selector=>selector.startsWith('#create-manual-settings') ? manual : [];
  const start=source.indexOf('function updateCreateKind()');
  const end=source.indexOf('$("#create-supervisor-mode").addEventListener',start);
  const context={state,$,$$,isMonthlyCreate:()=>false,automaticCreate:()=>!$('#create-manual-settings').open&&!state.humanObjective};
  vm.runInNewContext(source.slice(start,end),context);
  return {$,state,update:context.updateCreateKind};
}

test('description mode bypasses hidden required fields; manual mode restores them',()=>{
  const h=contextForm();
  h.update();
  assert.equal(h.$('#create-patient').disabled,true);
  assert.equal(h.$('#create-submit').disabled,false);
  assert.equal(h.$('#create-submit').textContent,'Review supervisor plan');
  h.$('#create-manual-settings').open=true;
  h.update();
  assert.equal(h.$('#create-patient').disabled,false);
  assert.equal(h.$('#create-provider').disabled,false);
  assert.equal(h.$('#create-workflow').disabled,false);
});

test('human clarification keeps its routing locked after leaving description mode',()=>{
  const h=contextForm();
  h.update();
  h.state.humanObjective={};
  h.$('#create-manual-settings').open=true;
  h.update();
  assert.equal(h.$('#create-agent').disabled,true);
  assert.equal(h.$('#create-workflow').disabled,true);
});

test('task labels prefer agent titles without changing the original assignment',()=>{
  const start=source.indexOf('const escapeHTML =');
  const end=source.indexOf('const pretty =',start);
  const {escapeHTML,taskTitle}=vm.runInNewContext(source.slice(start,end)+'; ({escapeHTML,taskTitle})');
  const task={objective:'Callback demo: collect remaining records',progress:{display_title:'Collect outstanding hospital records'}};
  assert.equal(taskTitle(task),'Collect outstanding hospital records');
  assert.equal(task.objective,'Callback demo: collect remaining records');
  assert.equal(escapeHTML('local-operator'),'User');
  assert.equal(escapeHTML('Local operator'),'User');
  assert.equal(escapeHTML('Callback demo: collect records'),'Callback collect records');
  assert.equal(escapeHTML('<script>'),'&lt;script&gt;');
});

test('a registered agent plan renders its name and reviewed inputs without built-in workflow fields', () => {
  const start = source.indexOf('function assignmentPlanHTML(draft)');
  const end = source.indexOf('\n}', start) + 2;
  const helpers = source.slice(source.indexOf('const escapeHTML ='), source.indexOf('const taskTitle ='));
  const draft = {
    contacts: [], capabilities: [{agent_key:'receipt_test', name:'Receipt confirmation'}],
    plan: {status:'ready', summary:'Confirm the requested reference', assumptions:[], questions:[],
      steps:[{kind:'agent',agent_key:'receipt_test',objective:'Confirm receipt',
        assignment_json:JSON.stringify({reference:'<R-17>'})}]},
  };
  const html = vm.runInNewContext(`${helpers}\n${source.slice(start, end)}\nassignmentPlanHTML(draft)`, {
    draft, statusName:value=>value.replaceAll('_',' '),
  });
  assert.match(html, /Receipt confirmation/);
  assert.match(html, /&lt;R-17&gt;/);
  assert.match(html, /Approve & start/);
  assert.doesNotMatch(html, /undefined/);
});

test('execution trace links accept http dashboards and reject executable URLs', () => {
  const start = source.indexOf('function executionTraceLinks(run)');
  const end = source.indexOf('\n}', start) + 2;
  const html = vm.runInNewContext(`${source.slice(start,end)}\nexecutionTraceLinks(run)`, {
    e: value=>value,
    run:{observability:{jaeger_url:'http://localhost:16686/trace/abc',langfuse_url:'javascript:alert(1)'}},
  });
  assert.match(html, /Open execution trace/);
  assert.match(html, /noopener noreferrer/);
  assert.doesNotMatch(html, /javascript|Open LLM trace/);
});
