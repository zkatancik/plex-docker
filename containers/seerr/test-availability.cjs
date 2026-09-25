const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Exercise the actual compiled upstream processShow, mocking only its I/O.
async function run(source) {
  const status = { UNKNOWN: 1, PROCESSING: 3, PARTIALLY_AVAILABLE: 4, AVAILABLE: 5, DELETED: 6 };
  const cases = [
    { name: 'expanded season', count: 10, total: 30, expected: 4 },
    { name: 'second partial scan', count: 10, total: 30, expected: 4, initial: 4 },
    { name: 'season completes', count: 30, total: 30, expected: 5, initial: 4 },
    { name: 'empty scan retains evidence', count: 0, total: 30, expected: 5 },
    { name: 'unknown total retains evidence', count: 10, total: 0, expected: 5 },
    { name: 'non-priority mode', count: 10, total: 30, expected: 5, priority: false },
    { name: 'Plex cannot override Arr', count: 10, total: 30, expected: 5, options: { ratingKey: '123' } },
    { name: 'Jellyfin cannot override Arr', count: 10, total: 30, expected: 5, options: { jellyfinMediaId: 'abc' } },
    { name: '4K partial season', count: 10, total: 30, expected: 4, is4k: true },
  ];
  for (const test of cases) {
    const field = test.is4k ? 'status4k' : 'status';
    const other = test.is4k ? 'status' : 'status4k';
    const media = { status: 5, status4k: 5, seasons: [{ seasonNumber: 1, status: 5, status4k: 5 }] };
    media[field] = media.seasons[0][field] = test.initial ?? 5;
    let saves = 0;
    const settings = { main: { prioritizeRadarrSonarr: test.priority ?? true } };
    class Entity { constructor(values) { Object.assign(this, values); } }
    const modules = {
      '../../api/themoviedb': class {},
      '../../constants/media': { MediaStatus: status, MediaType: { TV: 'tv' } },
      '../../datasource': { getRepository: () => ({ findOne: async () => media, save: async () => { saves++; } }) },
      '../../entity/Media': Entity,
      '../../entity/Season': Entity,
      '../../lib/settings': { getSettings: () => settings },
      '../../logger': { debug() {}, info() {}, warn() {}, error() {} },
      '../../utils/asyncLock': class { dispatch(_key, callback) { return callback(); } },
      crypto: require('node:crypto'),
    };
    const context = { exports: {}, require: (name) => {
      assert.ok(name in modules, `Unexpected dependency: ${name}`);
      return modules[name];
    } };
    vm.runInNewContext(source, context);
    const scanner = new context.exports.default('regression');
    scanner.enable4kShow = true;
    await scanner.processShow(325762, 479465, [{
      seasonNumber: 1,
      episodes: test.is4k ? 0 : test.count,
      episodes4k: test.is4k ? test.count : 0,
      totalEpisodes: test.total,
      is4kOverride: Boolean(test.is4k),
    }], test.options ?? {});
    assert.equal(media.seasons[0][field], test.expected, `${test.name}: season`);
    assert.equal(media[field], test.expected, `${test.name}: media`);
    assert.equal(media[other], 5, `${test.name}: other resolution`);
    assert.equal(saves, 1, `${test.name}: persisted`);
  }
  console.log(`Seerr scanner regression: ${cases.length} cases passed`);
}

if (require.main === module) {
  run(fs.readFileSync(process.argv[2], 'utf8')).catch(error => { console.error(error); process.exit(1); });
}
module.exports = { run };
