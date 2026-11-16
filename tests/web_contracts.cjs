// Execute the real TypeScript modules without driving a browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const root = path.resolve(__dirname, '../apps/web');

function moduleAt(file, overrides = {}, globals = {}) {
  const output = ts.transpileModule(fs.readFileSync(path.join(root, file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX }
  }).outputText;
  const exports = {};
  const context = { exports, require: name => name in overrides ? overrides[name] : require(name),
    console, URLSearchParams, AbortSignal, AbortController, setTimeout, clearTimeout, ...globals };
  vm.runInNewContext(output, context, { filename: file });
  return exports;
}

test('personal board responses are never reused as another response', async () => {
  let calls = 0;
  const api = moduleAt('lib/browserApi.ts', {}, { fetch: async () => ({ ok: true, json: async () => ({ rows: [++calls] }) }) });
  assert.equal((await api.browserGet('/v1/props')).rows[0], 1);
  assert.equal((await api.browserGet('/v1/props')).rows[0], 2);
  assert.equal(api.peekBrowserCache('/v1/props'), null);
});

test('failed refresh rejects instead of inventing an empty successful board', async () => {
  const api = moduleAt('lib/browserApi.ts', {}, { fetch: async () => ({ ok: false }) });
  await assert.rejects(api.browserGet('/v1/anytime-td/replay'), /temporarily unavailable/);
});

test('rendered prop card preserves the actual saved wager and pending result', () => {
  let displayed;
  const { PropDeskPanel } = moduleAt('components/PropDeskPanel.tsx', {
    '@/components/LockedCall': { LockedCall: () => null },
    '@/components/PropDesk': { DeskRecord: () => null, PropDesk: ({ calls }) => { displayed = calls; return null; } },
    '@/lib/browserApi': { peekBrowserCache: () => null }
  });
  const original = { player_name: 'Cooper Kupp', market_id: 'receptions', line: 2.5,
    book_price: -120, p: .55, result: 'pending', game_status: 'scheduled' };
  renderToStaticMarkup(React.createElement(PropDeskPanel, { league: 'nfl', date: '2099-11-15', initial: [original] }));
  assert.deepEqual(displayed, [original]);
});

test('customer pages do not import server-independent grading or bundled pick overrides', () => {
  function scan(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const file = path.join(dir, entry.name);
      if (entry.isDirectory()) scan(file);
      else if (/\.tsx?$/.test(file)) {
        const source = fs.readFileSync(file, 'utf8');
        assert.doesNotMatch(source, /from ["']@\/lib\/(?:shortCatch|gradeProps|gradeCalls|ats\/board\.json|savedBoard|diskJson|atdAlternate)["']/, file);
      }
    }
  }
  scan(path.join(root, 'app'));
  scan(path.join(root, 'components'));
});

test('ATS day preserves frozen winning selection and never fills an empty API response', async () => {
  const api = moduleAt('lib/atsDay.ts', { '@/lib/format': { kickoffDate: () => '2026-11-15' } });
  const row = { game_id: 'g', kickoff_utc: '2026-11-15T18:00:00Z', pick_side: 'away',
    spread: 3, result: 'win', locked: true };
  assert.equal((await api.callsForDate([row], '2026-11-15', 'all'))[0], row);
  assert.equal((await api.callsForDate([], '2026-11-15', 'all')).length, 0);
});
