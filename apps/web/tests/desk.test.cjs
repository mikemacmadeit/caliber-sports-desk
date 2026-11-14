const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const Module = require('node:module');
const m = new Module(__filename);
m._compile(ts.transpileModule(fs.readFileSync('apps/web/lib/desk.ts', 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText, __filename);
const {isCurrent, priceCheck} = m.exports;
const now = Date.parse('2026-11-14T17:00:00Z');
const row = {kickoff: '2026-11-14T18:00:00Z', qualification: {eligible: true, valid_until: '2026-11-14T17:12:00Z', reasons: [], minimum_american_odds: -105}};
test('a price expires on the client even without another successful request', () => {
 assert.equal(isCurrent(row, now), true);
 assert.equal(isCurrent(row, now + 12 * 60000), false);
 assert.equal(priceCheck(row, '+150', now + 12 * 60000).value, null);
});
test('checks actual entered odds against the same selection threshold', () => {
 assert.equal(priceCheck(row, '-110', now).value, false);
 assert.equal(priceCheck(row, '-105', now).value, true);
 assert.equal(priceCheck(row, '+110', now).value, true);
});
test('malformed odds and missing qualification cannot pass', () => {
 for (const value of ['', '0', '90', 'NaN', '1e3', '110.1']) assert.equal(priceCheck(row, value, now).value, null);
 assert.equal(priceCheck({...row, qualification: {...row.qualification, eligible:false}}, '+500', now).value, null);
});
test('a game at kickoff is no longer current', () => {
 assert.equal(isCurrent({...row, kickoff: new Date(now).toISOString()}, now), false);
});
