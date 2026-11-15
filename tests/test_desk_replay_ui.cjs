// Render the real card component without a browser or additional dependencies.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const ts = require('typescript');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const input = fs.readFileSync('apps/web/components/AnytimeTdBoard.tsx', 'utf8');
const compiled = ts.transpileModule(input, {compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020,
}}).outputText;
const mocked = {
  'next/link': {default: props => React.createElement('a', props)},
  '@/components/PropExplorer': {PropExplorer: () => null},
  '@/components/TeamMark': {TeamMark: () => null},
  '@/lib/days': {},
  '@/lib/browserApi': {},
  '@/lib/atdDeskTakes': {deskPickReason: () => 'Recorded pregame comparison'},
  '@/lib/format': {kickoffParts: () => ({day:'Nov 14',time:'11:00 AM'}), pct: p => `${Math.round(p*100)}%`, american: p => p == null ? '—' : p > 0 ? `+${p}` : String(p)},
  '@/lib/teams': {presentTeam: () => ({abbr:'TEST'})},
};
const context = {exports: {}, require: name => mocked[name] || require(name)};
vm.runInNewContext(compiled + '\nexports.TestCard = DeskPickCard; exports.TestDaily = DailyTdResults;', context);
const base = {game_id:'g', league_id:'fbs', player_name:'Runner', team:'TEST', forecast:{p_estimate:.6}, book_price:200, retrospective:true};
for (const [result, color, symbol] of [['scored','green','✓'], ['blank','red','✕']]) {
  const html = renderToStaticMarkup(React.createElement(context.exports.TestCard, {row:{...base,result,final_score:'14–21'}}));
  assert.ok(html.includes(`border-${color}-500`));
  assert.ok(html.includes(`bg-${color}-950/50`));
  assert.ok(html.includes('absolute right-3 top-3'));
  assert.ok(html.includes(symbol));
  assert.ok(html.includes('Final 14–21'));
  assert.ok(html.includes('Past-game rerun'));
}
for (const result of ['pending','awaiting_box']) {
  const html = renderToStaticMarkup(React.createElement(context.exports.TestCard,{row:{...base,result}}));
  assert.ok(!html.includes('border-red-500'));
  assert.ok(!html.includes('border-green-500'));
  assert.ok(result === 'pending' ? !html.includes('Not settled yet') : html.includes('waiting for the player result'));
}
console.log('Replay card rendering passed: green/check, red/X, final score, replay labels, and ungraded states.');
const provisional = renderToStaticMarkup(React.createElement(context.exports.TestCard,{row:{...base,retrospective:false,selection_readiness:{status:'provisional',reasons:['Starting quarterback role is not verified']}}}));
assert.ok(provisional.includes('Check playing status and role before kickoff'));
const evidenceRow = {...base, desk_analysis: {matchup_features_active:true,
  reasons:['Opponent allows +0.15 passing EPA per play.'],
  risks:['Player-level scoring-area opportunities remain unknown.'],
  current_context:{weather:{wind_mps:6,precip_probability:.2}}}};
const evidenceHtml = renderToStaticMarkup(React.createElement(context.exports.TestCard,{row:evidenceRow}));
assert.ok(evidenceHtml.includes('passing EPA'));
assert.ok(evidenceHtml.includes('remain unknown'));
assert.ok(evidenceHtml.includes('has not changed this estimate'));
const lockedHtml = renderToStaticMarkup(React.createElement(context.exports.TestCard,{row:{...evidenceRow,locked:true}}));
assert.ok(!lockedHtml.includes('passing EPA'));
const scoutingRow = {...base, desk_analysis:{scouting_report:{headline:'Rushing-led scoring case',frozen_inputs_only:false,
  information_cutoff:'2026-11-14T16:00:00Z',role:['14 carries in the last two team games'],matchup:['Opponent allows 0.1 rushing EPA'],
  game_script:['Team expectation: 28 points'],checks:['Active status unconfirmed'],gaps:['Routes unavailable'],
  alternatives:[{player_id:'alt',name:'Other Runner',team:'OPP',probability:.52,team_implied_points:35,
    price:{american:120},role:'Last two team games: 12 carries'}],comparison:'Larger team total needs individual share',
  price:{fair_american:-150,american:200,break_even:1/3,estimated_edge:.6-1/3,note:'Not a validated betting advantage'},
  model_note:'Context only',sources:[{label:'nflverse',url:'https://github.com/nflverse/nflverse-data'}]}}};
const scoutingHtml=renderToStaticMarkup(React.createElement(context.exports.TestCard,{row:scoutingRow}));
assert.ok(scoutingHtml.includes('<article') && scoutingHtml.includes('<details') && scoutingHtml.includes('<summary'));
assert.ok(scoutingHtml.includes('Other Runner') && scoutingHtml.includes('26.7 percentage points'));
assert.ok(!/<a\b[^>]*>(?:(?!<\/a>)[\s\S])*<details/.test(scoutingHtml),'Expanding analysis must not navigate through a wrapping link');
const lockedScoutingHtml=renderToStaticMarkup(React.createElement(context.exports.TestCard,{row:{...scoutingRow,locked:true}}));
assert.ok(!lockedScoutingHtml.includes('<details') && !lockedScoutingHtml.includes('Other Runner'));
console.log('Scouting report rendering passed: expandable case, alternatives, value, no wrapping link, and subscription gate.');
const readable = {version:'customer_td_v1',label:'Most likely scorer',chance:'60% chance',
  evidence:['With the current quarterback he led the team in red-zone targets.'],
  why:'The runner gets most of the carries near the goal line.',matchup:'The opponent has allowed running backs to finish these chances.',
  caution:'A teammate also gets carries.',price:'A winning $10 bet earns $20 profit.',comparison:null,research_note:'Estimates are being evaluated.'};
const twoChoiceRow={...scoutingRow,retrospective:false,desk_analysis:{...scoutingRow.desk_analysis,
  scouting_report:{...scoutingRow.desk_analysis.scouting_report,customer:readable}},
  decision:{value_candidate:{player_id:'alt',name:'Other Receiver',team:'TEST',position:'TE',probability:.4,american:250,price_supported:true,
    research:{opponent_games:6,quarterback:{usable:true,name:'Current QB',games:6,player_redzone_targets:8,receiving_tds:4},
      opponent:{targets:60,receiving_yards:445,receiving_tds:3,carries:0,rushing_yards:0,rushing_tds:0}},
    customer:{...readable,label:'Better-priced alternative',why:'The receiver has recent end-zone targets.',matchup:'This defense has struggled with those throws.',caution:'He gets fewer opportunities.'}},alternatives:[]},
  decision_results:{value_candidate:{result:'blank'}}};
const readableHtml=renderToStaticMarkup(React.createElement(context.exports.TestCard,{row:twoChoiceRow}));
assert.ok(readableHtml.includes('Estimated chance to score') && readableHtml.includes('Saved odds'));
assert.ok(readableHtml.includes('More analysis') && readableHtml.includes('With the current quarterback'));
// Native details stay closed initially. Verify useful facts are visible while
// the longer explanation, payout lesson and risk discussion remain available.
function collapsed(html) {
  let depth=0, visible='';
  for (const token of html.split(/(<\/?details\b[^>]*>)/)) {
    if (/^<details\b/.test(token)) depth++;
    else if (/^<\/details>/.test(token)) depth--;
    else if (!depth) visible+=token;
  }
  return visible;
}
const visible=collapsed(readableHtml);
assert.equal((visible.match(/aria-label="Key reasons"/g)||[]).length,2);
assert.equal((visible.match(/<li\b/g)||[]).length,4);
assert.ok(visible.includes('8 red-zone targets and 4 receiving TDs with Current QB across 6 games'));
assert.ok(visible.includes('3 receiving TDs and 74 yards per game to tight ends across 6 games'));
assert.ok(!visible.includes('A winning $10 bet') && !visible.includes('He gets fewer opportunities'));
assert.ok(!readableHtml.includes('<details open'));
const staleHtml=renderToStaticMarkup(React.createElement(context.exports.TestCard,{row:{...twoChoiceRow,decision:{...twoChoiceRow.decision,
  value_candidate:{...twoChoiceRow.decision.value_candidate,price_supported:false,price_status:'no_fresh_price',minimum_american:190}}}}));
assert.ok(collapsed(staleHtml).includes('Other Receiver') && collapsed(staleHtml).includes('Check current odds') && collapsed(staleHtml).includes('+190'));
assert.ok(readableHtml.includes('Better-priced alternative') && readableHtml.includes('recent end-zone targets'));
assert.ok(readableHtml.includes('He gets fewer opportunities') && readableHtml.includes('border-red-500'));
assert.ok(!readableHtml.includes('Touchdown ?') && !readableHtml.includes('chance ?'));
const lockedTwo=renderToStaticMarkup(React.createElement(context.exports.TestCard,{row:{...twoChoiceRow,locked:true}}));
assert.ok(!lockedTwo.includes('Other Receiver') && !lockedTwo.includes('recent end-zone targets'));
console.log('Two-choice card passed: four concise reasons, collapsed research, visible odds warnings, separate results, and protected pregame details.');
const selectorSource = fs.readFileSync('apps/web/lib/atdDeskTakes.ts','utf8');
const selectors = {exports:{}};
vm.runInNewContext(ts.transpileModule(selectorSource,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,selectors);
const candidate = { ...base, player_id:'p', price_freshness:{is_current_opportunity:true},
  forecast:{p_estimate:.6,availability_status:'active'}, desk_analysis:{eligible:true,combination_status:'active'},
  market_context:{usable:true}, team_implied_points:28 };
assert.equal(selectors.exports.selectDeskTakes([candidate]).length,1);
assert.equal(selectors.exports.selectDeskTakes([{...candidate,book_price:null}]).length,0);
assert.equal(selectors.exports.selectDeskTakes([{...candidate,team_implied_points:18}]).length,0);
assert.equal(selectors.exports.selectDeskTakes([{...candidate,market_context:{usable:false}}]).length,0);
console.log('Frontend selector passed: missing price, weak team total, and missing context all pass.');
const guarded = {...candidate, selection_safety:{eligible:true}, book_price:-250,forecast:{...candidate.forecast,p_estimate:.8}};
const cheaper = {...guarded,player_id:'cheap',book_price:110,forecast:{...guarded.forecast,p_estimate:.71}};
assert.equal(selectors.exports.selectDeskTakes([guarded,cheaper])[0].player_id,'p');
assert.equal(selectors.exports.selectDeskTakes([{...guarded,selection_safety:{eligible:false}}]).length,0);
console.log('Prospective selector passed: blocked candidates excluded, highest qualifying chance retained.');
const dailyHtml = renderToStaticMarkup(React.createElement(context.exports.TestDaily, {
  rows:[{...base,result:'scored'},{...base,result:'blank'},{...base,result:'awaiting_box'}],
  date:'2026-11-14',filter:'all',onFilter:()=>{},
}));
assert.ok(dailyHtml.includes('1 winners') && dailyHtml.includes('1 losers'));
assert.ok(dailyHtml.includes('50% hit rate across 2 graded games'));
assert.ok(dailyHtml.includes('1 final games await'));
console.log('Daily summary passed: ungraded players are excluded from hit rate.');

async function checkIndependentLoading() {
  const effects=[], updates=[], pending={}; let stateIndex=0;
  const hookReact={...React,useState:initial=>{const index=stateIndex++;return [initial,value=>updates.push([index,value])];},useEffect:fn=>effects.push(fn),useMemo:fn=>fn()};
  const mockApi={peekBrowserCache:()=>null,browserGet:path=>new Promise((resolve,reject)=>{pending[path]={resolve,reject};})};
  const isolated={exports:{},URLSearchParams,AbortController,require:name=>name==='react'?hookReact:name==='@/lib/browserApi'?mockApi:mocked[name]||require(name)};
  vm.runInNewContext(compiled,isolated);
  isolated.exports.AnytimeTdBoard({league:'all',date:'2026-11-14',today:'2026-11-14',candidateDates:[],emptyTitle:'Empty'});
  effects[1]();
  const replayPath=Object.keys(pending).find(p=>p.includes('/replay'));
  const pricesPath=Object.keys(pending).find(p=>p.includes('/props'));
  pending[replayPath].resolve({rows:[base]});
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(updates.some(([i,v])=>i===1&&v.length===1),'Scorer cards must load while prices remain pending');
  assert.ok(updates.some(([i,v])=>i===6&&v===false),'Price latency must not hold the loading screen');
  pending[pricesPath].reject(new Error('Price feed down'));
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(!updates.some(([i,v])=>i===1&&v.length===0),'Price failure must not clear scorer cards');
  console.log('Independent loading passed: slow/failed odds cannot hide successful scorer picks.');
}
checkIndependentLoading().catch(error=>{console.error(error);process.exitCode=1;});
