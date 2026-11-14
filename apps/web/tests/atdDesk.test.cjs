const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const Module = require('node:module');
const m = new Module(__filename);
m._compile(ts.transpileModule(fs.readFileSync('apps/web/lib/atdDeskTakes.ts', 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText, __filename);
const {selectDeskTakes, deskPickReason} = m.exports;
const row = (id, p=.5) => ({outcome_id:id, player_id:id, player_name:id, game_id:'g', book_price:100, forecast:{p_estimate:p,availability_status:'active'}});
test('a filtered nonselection never becomes the Desk pick', () => {
 const nonselection = {...row('backup'), desk_selected:false};
 assert.deepEqual(selectDeskTakes([nonselection]), []);
 const selected = {...row('starter', .7), desk_selected:true};
 assert.deepEqual(selectDeskTakes([nonselection,selected]).map(r=>r.player_id), ['starter']);
});
test('questionable and invalid estimates are excluded', () => {
 for (const status of ['questionable','inactive','confirmed_inactive','out','doubtful']) {
  const r=row('p'); r.forecast.availability_status=status;
  assert.equal(selectDeskTakes([r]).length,0);
 }
 for (const p of [NaN,Infinity,1.3,.21]) assert.equal(selectDeskTakes([row('p',p)]).length,0);
});
test('ties do not depend on sorting and short prices do not decide a scorer', () => {
 const rows=[row('a'), {...row('b'),book_price:-900}];
 assert.equal(selectDeskTakes(rows)[0].player_id,'b');
 assert.equal(selectDeskTakes(rows.reverse())[0].player_id,'b');
});
test('frozen picks remain displayed and evidence is the supplied measured explanation', () => {
 const r={...row('p'), locked:true, desk_selected:true, desk_analysis:{eligible:false,reasons:['2.0 goal-line carries per game.']}};
 assert.equal(selectDeskTakes([r]).length,1);
 assert.equal(deskPickReason(r),'2.0 goal-line carries per game.');
});
