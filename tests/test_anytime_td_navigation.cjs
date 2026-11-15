// Exercise the page's date selection and its real league/date links.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const ts = require('typescript');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');

function load(path, dependencies = {}) {
  const source = ts.transpileModule(fs.readFileSync(path, 'utf8'), {compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020,
  }}).outputText;
  const context = {exports: {}, URLSearchParams, require: name => dependencies[name] || require(name)};
  vm.runInNewContext(source, context);
  return context.exports;
}

const today = '2026-11-14';
const link = {default: ({children, ...props}) => React.createElement('a', props, children)};
const days = load('apps/web/lib/days.ts');
const common = {'next/link': link, '@/lib/days': {...days, slateToday: () => today}};
const dayStrip = load('apps/web/components/DayStrip.tsx', common);
const leagueToggle = load('apps/web/components/LeagueToggle.tsx', common);
const page = load('apps/web/app/anytime-td/page.tsx', {
  ...common,
  '@/components/DayStrip': dayStrip,
  '@/components/LeagueToggle': leagueToggle,
  '@/components/PageHeader': {PageHeader: ({action}) => action},
  '@/components/AnytimeTdRecord': {AnytimeTdRecord: () => null},
  '@/components/AnytimeTdResearchBoard': {AnytimeTdResearchBoard: () => null},
  '@/components/AnytimeTdBoard': {AnytimeTdBoard: ({date, league}) => React.createElement('div', {'data-board': `${league}:${date}`})},
  '@/lib/api': {apiGet: async path => path.startsWith('/v1/days')
    ? {today, timezone: 'America/Chicago', dates: path.endsWith('=nfl') ? ['2026-11-16', '2026-11-15'] : [today, '2026-11-15']}
    : {}},
}).default;

async function run() {
  const render = async params => renderToStaticMarkup(await page({searchParams: Promise.resolve(params)}));
  const nfl = await render({league:'nfl'});
  assert.ok(nfl.includes('data-board="nfl:2026-11-15"'), 'NFL opens the nearest upcoming game day');
  assert.ok(nfl.includes('No NFL games today. Showing Sun, Nov 15 picks.'));
  assert.ok(nfl.includes('href="/anytime-td?league=fbs"'), 'An automatic NFL date must not stick when switching leagues');
  assert.ok(nfl.includes('href="/anytime-td?league=nfl&amp;date=2026-11-14"'), 'Today remains explicitly selectable');
  const explicitToday = await render({league:'nfl', date:today});
  assert.ok(explicitToday.includes('No games today') && !explicitToday.includes('data-board='));
  assert.ok(explicitToday.includes('href="/anytime-td?league=fbs&amp;date=2026-11-14"'), 'An explicitly chosen day survives league switching');
  const previousDay = await render({league:'nfl', date:'2026-11-13'});
  assert.ok(previousDay.includes('No games Fri, Nov 13') && !previousDay.includes('data-board='));
  const explicitSunday = await render({league:'nfl', date:'2026-11-15'});
  assert.ok(explicitSunday.includes('data-board="nfl:2026-11-15"') && !explicitSunday.includes('No NFL games today. Showing'));
  for (const league of ['fbs','all']) {
    assert.ok((await render({league})).includes(`data-board="${league}:${today}"`), 'Today stays selected when it has games');
  }
  console.log('Anytime TD navigation passed: upcoming NFL default, today/previous dates, and league changes.');
}
run().catch(error => {console.error(error); process.exitCode = 1;});
