const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('src/agents_platform/static/app.js','utf8');
test('subtasks column links to original child tasks and escapes titles',()=>{
 const start=source.indexOf('function subtasksHTML(');
 const ctx={e:s=>String(s).replaceAll('<','&lt;').replaceAll('"','&quot;'), taskTitle:t=>t.objective};
 vm.runInNewContext(source.slice(start,source.indexOf('function renderTasks(',start)),ctx);
 const html=ctx.subtasksHTML({subtasks:[{id:'child',objective:'<script>Patient</script>'}]});
 assert.match(html,/data-task="child"/);
 assert.match(html,/aria-label="Open subtask:/);
 assert.doesNotMatch(html,/<script>/);
 assert.match(ctx.subtasksHTML({}),/—/);
 assert.match(source,/<th scope="col">Subtasks<\/th>/);
});
