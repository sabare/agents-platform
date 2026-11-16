const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('src/agents_platform/static/app.js','utf8');
function emptyState(status,search=''){
  const context={state:{overview:{supervisors:[{case_id:'case',group_task_id:'saved-assignment',status}]}},
    $:selector=>({value:selector==='#search'?search:''}),e:String};
  const start=source.indexOf('function pendingAssignmentsHTML()');
  vm.runInNewContext(source.slice(start,source.indexOf('function renderTasks(',start)),context);
  return context.pendingAssignmentsHTML();
}
test('saved assignment remains visible when specialist creation needs review',()=>{
  const html=emptyState('NEEDS_REVIEW');
  assert.match(html,/Assignment needs review/);
  assert.match(html,/data-task="saved-assignment"/);
  assert.doesNotMatch(html,/Create a task/);
});
test('pending supervisor is shown without overriding an active search',()=>{
  assert.match(emptyState('PENDING'),/Assignment saved/);
  assert.equal(emptyState('PENDING','unrelated'), '');
  assert.equal(emptyState('SATISFIED'), '');
});
