const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../app/static/workspace.js'), 'utf8');
const context = {
  cardData: [{jobId:'live',status:'downloading',title:'Live'}, {status:'ready',title:'Draft'}],
  tasks: [{id:'live',status:'downloading'},{id:'past',status:'done',title:'Past'}],
  activeStates: new Set(['downloading','paused','queued']), attentionStates: new Set(['error','info-error','missing']),
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  function collect()'), source.indexOf('  function mediaDescription')), context);
const entries = context.collect();
assert.equal(entries.length,3,'A download shared by a card and history must appear only once');
assert.equal(entries.filter(e=>e.task?.id==='live').length,1);
assert.equal(entries.find(e=>e.title==='Draft').card.status,'ready');
assert.equal(entries.filter(e=>context.matches(e,'active')).length,1,'Ready drafts must not count as active downloads');
assert.equal(entries.filter(e=>context.matches(e,'done')).length,1);
context.tasks[0].status='error';
assert.equal(context.collect().filter(e=>context.matches(e,'attention')).length,1,'Server errors determine the live task category');
context.cardData=[];
assert.equal(context.collect().length,2,'History survives loss of renderer draft state');
console.log('OK unified task identity, category changes, and persisted history');
context.attr = String;
context.uiIcon = () => '';
context.fmtSize = size => String(size);
vm.runInContext(source.slice(source.indexOf('  function mediaDescription'), source.indexOf('  function syncSidebarAccessibility')), context);
const draft = {format:'video', preset:'smallest',formats:[{id:'high',height:1080,label:'1080p SDR'},{id:'low',height:144,label:'144p SDR'}]};
assert(context.mediaDescription({card:draft}).includes('最低可用画质'));
assert(!context.mediaDescription({card:draft}).includes('1080p'));
for (const status of ['error','cancelled']) {
  const markup = context.rowActions({status,key:'card:0',index:0,card:draft,task:{id:'old',preset:'recommended'}});
  assert(markup.includes('data-click-action="download-card"'), 'Retry must read edited card settings');
  assert(!markup.includes('data-task-action="retry"'));
}
console.log('OK edited retries and honest quality summaries');
