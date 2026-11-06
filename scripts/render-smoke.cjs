// Render the real page components with the live local API contract, without a
// remote-debugging connection or changes to managed browser policy.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const ts = require('typescript');
const React = require('react');
const { renderToString } = require('react-dom/server');
const { MemoryRouter } = require('react-router-dom');
require.extensions['.css'] = () => {};
for (const extension of ['.tsx', '.ts']) {
  require.extensions[extension] = function (module, filename) {
    const source = fs.readFileSync(filename, 'utf8');
    const output = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
      fileName: filename,
    });
    module._compile(output.outputText, filename);
  };
}
const pages = require('../src/pages.tsx');
const EquitiesPage = require('../src/EquitiesPage.tsx').default;
const FxPage = require('../src/FxPage.tsx').default;
const BondsPage = require('../src/BondsPage.tsx').default;
const RatesPage = require('../src/RatesPage.tsx').default;
const { BopContent } = require('../src/BopSection.tsx');
const { normalizeRows } = require('../src/bopNormalize.ts');
const chartModule = require('../src/BopChart.tsx');
const RealBopChart = chartModule.default;
let renderedCharts = [];
chartModule.default = function CaptureBopChart(props) {
  renderedCharts.push(props);
  return React.createElement(RealBopChart, props);
};
async function main() {
  const response = await fetch('http://127.0.0.1:8000/api/workspace');
  if (!response.ok) throw new Error(`Workspace API failed: ${response.status}`);
  const data = await response.json();
  const props = { data, reload: async () => {}, notify: () => {}, onOpen: () => {} };
  const cases = [
    ['Overview', pages.Overview, {}],
    ['bonds', BondsPage, {}],
    ['rates', RatesPage, {}],
    ['commodities', pages.AssetPage, { asset: 'commodities' }],
    ['equities', EquitiesPage, {}],
    ['fx', FxPage, {}],
    ['Research', pages.ResearchPage, {}],
    ['Portfolio', pages.PortfolioPage, {}],
    ['Backtests', pages.BacktestsPage, {}],
    ['Data', pages.DataPage, {}],
    ['Settings', pages.SettingsPage, {}],
  ];
  for (const [name, Page, extra] of cases) {
    const html = renderToString(React.createElement(MemoryRouter, null, React.createElement(Page, { ...props, ...extra })));
    if (!html.includes('<h1>') || html.length < 1000) throw new Error(`Incomplete render: ${name}`);
    console.log(`PASS ${name}: ${html.length} bytes of rendered markup`);
  }
  const bopResponse = await fetch('http://127.0.0.1:8000/api/fx/bop?country=USA&frequency=trailing4q&refresh=false');
  if (!bopResponse.ok) throw new Error(`BOP API failed: ${bopResponse.status}`);
  const bop = await bopResponse.json();
  assert.ok(bop.countries.some(country => country.code === 'AUS' && country.connected), 'Australia must be connected.');
  assert.ok(bop.countries.some(country => country.code === 'NZL' && country.connected), 'New Zealand must be connected.');
  assert.ok(bop.countries.some(country => country.code === 'CHE' && country.connected), 'Switzerland must be connected.');
  assert.ok(bop.countries.some(country => country.code === 'CAN' && country.connected), 'Canada must be connected.');
  assert.ok(!bop.countries.some(country => country.code === 'EUR'), 'Euro area must be removed.');
  if (bop.macro?.status !== 'ready' || bop.macro.sources.length !== 5 || !bop.macro.trade.length || !bop.macro.quarterly.length || bop.macro.frequency !== bop.frequency) {
    throw new Error('The BOP API did not supply the five official companion source families.');
  }
  const bopHtml = renderToString(React.createElement(BopContent, { data: bop, range: '10Y' }));
  const chartCount = (bopHtml.match(/class="panel bop-chart-panel"/g) || []).length;
  const zoomControls = (bopHtml.match(/aria-label="Reset zoom for /g) || []).length;
  const normalizeControls = (bopHtml.match(/aria-label="Normalize data for /g) || []).length;
  if (normalizeControls !== 3) throw new Error(`Expected three normalization controls, found ${normalizeControls}.`);
  if (!bopHtml.includes('Current account / GDP &amp; trade-weighted US dollar') || !bopHtml.includes('left: current account / GDP (%)') || !bopHtml.includes('right: broad dollar index')) throw new Error('Combined external-balance chart or axis descriptions are missing.');
  const tableRows = (bopHtml.match(/scope="row"/g) || []).length;
  if (chartCount !== 7 || zoomControls !== 7 || tableRows !== 0 || !bopHtml.includes(bop.latest.period)) {
    throw new Error(`Incomplete BOP render: ${chartCount} charts, ${tableRows} periods`);
  }
  if (!bopHtml.includes('Reserve-asset transactions &amp; reserves / short-term debt') || !bopHtml.includes('left: reserve transactions (USD bn)') || !bopHtml.includes('right: reserves / short-term debt (%)')) throw new Error('Combined reserve chart or axis descriptions are missing.');
  if (!bopHtml.includes('Exports, imports &amp; trade balance') || !bopHtml.includes('bars = trade balance') || !bopHtml.includes('Trade growth: year on year')) throw new Error('Combined monthly trade chart or separate growth chart is missing.');
  for (const removed of ['Monthly exports & imports', 'Monthly trade balance', 'Exchange rate: EUR / USD', 'Months of import coverage', 'The customs / BOP gap', 'Primary income: receipts vs. payments', 'External financing: two measures', 'Statistical discrepancy']) {
    if (bopHtml.includes(`>${removed}<`)) throw new Error(`Removed chart still displayed: ${removed}`);
  }
  console.log(`PASS loaded USA BOP: ${chartCount} charts, ${zoomControls} zoom controls, ${normalizeControls} normalization controls, ${bop.latest.period}`);
  for (const country of ['USA', 'JPN', 'CHN', 'GBR', 'CAN', 'AUS', 'NZL', 'CHE', 'SGP']) {
  for (const frequency of ['quarterly', 'trailing4q']) {
    const result = await fetch(`http://127.0.0.1:8000/api/fx/bop?country=${country}&frequency=${frequency}&refresh=false`);
    if (!result.ok) throw new Error(`Frequency request failed: ${frequency}`);
    const selected = await result.json();
    if (selected.macro.frequency !== frequency) throw new Error('Companion frequency differs from the selected filter.');
    const lastDate = [...selected.series, ...selected.macro.quarterly, ...selected.macro.trade].map(row => row.date).sort().at(-1);
    for (const range of ['1Y', '5Y', '10Y', 'All']) {
      renderedCharts = [];
      const html = renderToString(React.createElement(BopContent, {data: selected, range}));
      const count = (html.match(/class="panel bop-chart-panel"/g) || []).length;
      const description = frequency === 'quarterly' ? 'quarterly flow' : 'rolling 4-quarter sum';
      if (count !== 7 || !html.includes(description) || !html.includes('3Q average')) throw new Error(`Incomplete filter render: ${frequency} / ${range}`);
      assert.equal(renderedCharts.length, 7);
      if (country === 'JPN') {
        assert.ok(html.includes('trade-weighted Japanese yen') && html.includes('JPY bn') && html.includes('Japan Customs'));
        assert.ok(!/USD bn|US dollar|Broad dollar index|Census|Treasury/.test(html), 'Japan chart contains US labels.');
        assert.ok(html.includes('original maturity') && html.includes('separately published BOJ seasonally adjusted current account'));
        assert.ok(renderedCharts.every(chart => chart.rows.some(row => chart.plots.every(plot => typeof row[plot.key] === 'number'))), 'A Japan chart has no complete plotted observation.');
        assert.ok(renderedCharts.find(chart => chart.title.includes('Reserve-asset')).plots.some(plot => plot.bar));
        assert.ok(renderedCharts.find(chart => chart.title.includes('Reserve-asset')).plots.some(plot => plot.axis === 'right' && !plot.bar));
      }
      if (country === 'CHN') {
        assert.ok(html.includes('trade-weighted Chinese renminbi') && html.includes('USD bn') && renderedCharts.some(chart => chart.plots.some(plot => plot.name === 'GACC customs via NBS')));
        assert.ok(!/US dollar|Broad dollar index|Census|Treasury|JPY bn|Japanese yen/.test(html), 'China chart contains another country\'s labels.');
        assert.ok(html.includes('sector short-term debt') && html.includes('original maturity') && html.includes('intercompany lending'));
        assert.ok(html.includes('SAFE') && html.includes('matching NBS nominal GDP') && html.includes('NSA levels'));
        assert.ok(renderedCharts.every(chart => chart.rows.some(row => chart.plots.every(plot => typeof row[plot.key] === 'number'))), 'A China chart has no complete plotted observation.');
        assert.equal((html.match(/aria-label="Normalize data for /g) || []).length, 3);
      }
      if (country === 'GBR') {
        assert.ok(html.includes('trade-weighted sterling') && html.includes('GBP bn') && html.includes('£'));
        assert.ok(!/USD bn|US dollar|Broad dollar index|Census|Treasury|JPY bn|Japanese yen|SAFE|Chinese renminbi/.test(html), 'UK chart contains another country\'s labels.');
        assert.ok(html.includes('including precious metals') && html.includes('original maturity') && html.includes('intercompany lending'));
        assert.ok(html.includes('SA goods headline') && html.includes('both NSA') && html.includes('Bank of England'));
        const comparison = renderedCharts.find(chart => chart.title === 'Customs vs. BOP goods balance');
        assert.ok(comparison.plots.some(plot => plot.name === 'HMRC customs'));
        assert.ok(comparison.plots.some(plot => plot.key === 'bop_goods_nsa'));
        assert.ok(comparison.subtitle.endsWith('NSA'));
        assert.ok(renderedCharts.every(chart => chart.rows.some(row => chart.plots.every(plot => typeof row[plot.key] === 'number'))), 'A UK chart has no complete plotted observation.');
        assert.equal((html.match(/aria-label="Normalize data for /g) || []).length, 3);
      }
      if (country === 'AUS') {
        assert.ok(html.includes('trade-weighted Australian dollar') && html.includes('AUD bn') && html.includes('A$'));
        assert.ok(!/USD bn|US dollar|Broad dollar index|Census|Treasury|JPY bn|Japanese yen|SAFE|Chinese renminbi|HMRC|GBP bn|SGD bn/.test(html), 'Australia chart contains another country\'s labels.');
        assert.ok(html.includes('goods only') && html.includes('customs value') && html.includes('RBA'));
        const comparison = renderedCharts.find(chart => chart.title === 'Customs vs. BOP goods balance');
        assert.ok(comparison.plots.some(plot => plot.name === 'ABS merchandise'));
        assert.ok(comparison.plots.some(plot => plot.key === 'bop_goods_nsa'));
        assert.ok(comparison.subtitle.endsWith('NSA'));
        const investment = renderedCharts.find(chart => chart.title === 'Net inward investment');
        assert.deepEqual(investment.plots.map(plot => plot.key), ['net_direct', 'net_equity', 'net_debt', 'net_other']);
        assert.ok(renderedCharts.every(chart => chart.rows.some(row => chart.plots.every(plot => typeof row[plot.key] === 'number'))), 'An Australia chart has no complete plotted observation.');
        assert.equal((html.match(/aria-label="Normalize data for /g) || []).length, 3);
      }
      if (country === 'NZL') {
        assert.ok(html.includes('trade-weighted New Zealand dollar') && html.includes('NZD bn') && html.includes('NZ$'));
        assert.ok(!/USD bn|US dollar|Broad dollar index|Census|Treasury|JPY bn|Japanese yen|SAFE|Chinese renminbi|HMRC|GBP bn|SGD bn|AUD bn/.test(html), 'New Zealand chart contains another country\'s labels.');
        assert.ok(html.includes('goods only') && html.includes('re-exports') && html.includes('RBNZ') && html.includes('NSA'));
        const comparison = renderedCharts.find(chart => chart.title === 'Customs vs. BOP goods balance');
        assert.ok(comparison.plots.some(plot => plot.name === 'Stats NZ merchandise'));
        assert.ok(comparison.plots.some(plot => plot.key === 'goods'));
        const investment = renderedCharts.find(chart => chart.title === 'Net inward investment');
        assert.deepEqual(investment.plots.map(plot => plot.key), ['net_direct', 'net_equity', 'net_debt', 'net_other']);
        assert.ok(renderedCharts.every(chart => chart.rows.some(row => chart.plots.every(plot => typeof row[plot.key] === 'number'))), 'A New Zealand chart has no complete plotted observation.');
        assert.equal((html.match(/aria-label="Normalize data for /g) || []).length, 3);
      }
      if (country === 'CAN') {
        assert.ok(html.includes('trade-weighted Canadian dollar') && html.includes('CAD bn') && html.includes('C$'));
        assert.ok(!/USD bn|US dollar|Broad dollar index|Census|Treasury|JPY bn|Japanese yen|SAFE|Chinese renminbi|HMRC|GBP bn|SGD bn|AUD bn|NZD bn|CHF bn/.test(html), 'Canadian chart contains another country\'s labels.');
        assert.ok(html.includes('Statistics Canada') && html.includes('Bank of Canada CEER') && html.includes('original maturity') && html.includes('FOB'));
        assert.ok(html.includes('annual-rate GDP series is not used') && html.includes('does not separately identify derivatives'));
        const comparison = renderedCharts.find(chart => chart.title === 'Customs vs. BOP goods balance');
        assert.ok(comparison.plots.some(plot => plot.name === 'Statistics Canada customs'));
        assert.ok(comparison.plots.some(plot => plot.key === 'goods'));
        const investment = renderedCharts.find(chart => chart.title === 'Net inward investment');
        assert.deepEqual(investment.plots.map(plot => plot.key), ['net_direct', 'net_equity', 'net_debt', 'net_other']);
        assert.ok(renderedCharts.every(chart => chart.rows.some(row => chart.plots.every(plot => typeof row[plot.key] === 'number'))), 'A Canadian chart has no complete plotted observation.');
        assert.equal((html.match(/aria-label="Normalize data for /g) || []).length, 3);
      }
      if (country === 'CHE') {
        assert.ok(html.includes('trade-weighted Swiss franc') && html.includes('CHF bn') && html.includes('CHF '));
        assert.ok(!/USD bn|US dollar|Broad dollar index|Census|Treasury|JPY bn|Japanese yen|SAFE|Chinese renminbi|HMRC|GBP bn|SGD bn|AUD bn|NZD bn/.test(html), 'Swiss chart contains another country\'s labels.');
        assert.ok(html.includes('goods only') && html.includes('non-monetary gold') && /merchanting/i.test(html) && html.includes('total 1'));
        assert.ok(html.includes('SECO') && html.includes('original maturity') && html.includes('sport'));
        const comparison = renderedCharts.find(chart => chart.title === 'Customs vs. BOP goods balance');
        assert.ok(comparison.plots.some(plot => plot.name === 'FOCBS broad customs via SNB'));
        assert.ok(comparison.plots.some(plot => plot.key === 'goods'));
        const investment = renderedCharts.find(chart => chart.title === 'Net inward investment');
        assert.deepEqual(investment.plots.map(plot => plot.key), ['net_direct', 'net_equity', 'net_debt', 'net_other']);
        assert.ok(renderedCharts.every(chart => chart.rows.some(row => chart.plots.every(plot => typeof row[plot.key] === 'number'))), 'A Swiss chart has no complete plotted observation.');
        assert.equal((html.match(/aria-label="Normalize data for /g) || []).length, 3);
      }
      if (country === 'SGP') {
        assert.ok(html.includes('trade-weighted Singapore dollar') && html.includes('SGD bn') && html.includes('S$'));
        assert.ok(!/USD bn|US dollar|Broad dollar index|Census|Treasury|JPY bn|Japanese yen|SAFE|Chinese renminbi|HMRC|GBP bn/.test(html), 'Singapore chart contains another country\'s labels.');
        assert.ok(html.includes('including re-exports') && html.includes('goods only') && html.includes('BIS basket differs from MAS'));
        const investment = renderedCharts.find(chart => chart.title === 'Net inward investment');
        assert.deepEqual(investment.plots.map(plot => plot.key), ['net_direct', 'net_portfolio', 'net_other']);
        assert.ok(investment.note.includes('split is annual and is not interpolated'));
        assert.ok(renderedCharts.some(chart => chart.plots.some(plot => plot.name === 'Enterprise Singapore customs')));
        assert.ok(renderedCharts.every(chart => chart.rows.some(row => chart.plots.every(plot => typeof row[plot.key] === 'number'))), 'A Singapore chart has no complete plotted observation.');
        assert.equal((html.match(/aria-label="Normalize data for /g) || []).length, 3);
      }
      const years = range === '1Y' ? 1 : range === '5Y' ? 5 : 10;
      const cutoff = new Date(Date.UTC(Number(lastDate.slice(0, 4)) - years, Number(lastDate.slice(5, 7)) + 2, 1)).toISOString().slice(0, 10);
      for (const chart of renderedCharts) {
        const keys = chart.plots.map(plot => plot.key);
        const source = keys.includes('exports') || keys.includes('exports_yoy') ? selected.macro.trade : keys.includes('current_account') || keys.includes('customs_goods') ? selected.series : selected.macro.quarterly;
        const expected = range === 'All' ? source : source.filter(row => row.date >= cutoff && row.date <= lastDate);
        assert.deepEqual(chart.rows, expected, `Wrong observations plotted for ${chart.title}, ${frequency}, ${range}`);
        assert.ok(chart.rows.every(row => [1, 4, 7, 10].includes(Number(row.date.slice(5, 7)))), 'All plotted points must be calendar quarters.');
        if (chart.normalizable) {
          const original = JSON.stringify(chart.rows);
          const normalized = normalizeRows(chart.rows, keys);
          assert.equal(JSON.stringify(chart.rows), original, 'Normalization changed source observations.');
          for (const key of keys) {
            const values = chart.rows.map(row => row[key]).filter(value => typeof value === 'number' && Number.isFinite(value));
            const mean = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : 0;
            const deviation = values.length ? Math.sqrt(values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / values.length) : 0;
            const constant = values.every(value => value === values[0]);
            chart.rows.forEach((row, index) => {
              const value = row[key];
              const expectedScore = typeof value === 'number' && Number.isFinite(value) ? constant || !deviation ? 0 : (value - mean) / deviation : null;
              if (expectedScore === null) assert.equal(normalized[index][key], null);
              else assert.ok(Math.abs(normalized[index][key] - expectedScore) < 1e-8, `Incorrect normalized value: ${chart.title} / ${key}`);
            });
          }
        }
      }
      console.log(`PASS ${country} BOP filters: ${frequency} / ${range}: all ${count} charts`);
    }
  }
  }
  console.log('All 11 pages and loaded BOP content rendered successfully. This does not verify browser layout or interactions.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
