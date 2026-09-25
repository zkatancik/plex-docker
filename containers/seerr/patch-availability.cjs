const fs = require('node:fs');

// Only a positive, incomplete count from the authoritative Sonarr scanner can
// invalidate an earlier complete season. Empty scans and other resolutions must
// retain the upstream protection against competing scanners.
function confirmedPartial(season, is4k, authoritative) {
  const count = is4k ? season.episodes4k : season.episodes;
  return authoritative && Boolean(season.is4kOverride) === is4k &&
    season.seasonNumber !== 0 && count > 0 && season.totalEpisodes > count;
}

function patch(source) {
  if (source.includes('function confirmedPartial(')) {
    throw new Error('Seerr availability patch is already applied');
  }
  const edits = [
    ['existingSeason.status === media_1.MediaStatus.AVAILABLE',
      '(existingSeason.status === media_1.MediaStatus.AVAILABLE && !confirmedPartial(season, false, settings.main.prioritizeRadarrSonarr && !isFromMediaServer))'],
    ['existingSeason.status4k === media_1.MediaStatus.AVAILABLE',
      '(existingSeason.status4k === media_1.MediaStatus.AVAILABLE && !confirmedPartial(season, true, settings.main.prioritizeRadarrSonarr && !isFromMediaServer))'],
    ['const shouldStayAvailable = media.status === media_1.MediaStatus.AVAILABLE &&',
      'const shouldStayAvailable = !seasons.some(s => confirmedPartial(s, false, settings.main.prioritizeRadarrSonarr && !isFromMediaServer)) && media.status === media_1.MediaStatus.AVAILABLE &&'],
    ['const shouldStayAvailable4k = media.status4k === media_1.MediaStatus.AVAILABLE &&',
      'const shouldStayAvailable4k = !seasons.some(s => confirmedPartial(s, true, settings.main.prioritizeRadarrSonarr && !isFromMediaServer)) && media.status4k === media_1.MediaStatus.AVAILABLE &&'],
  ];
  for (const [before, after] of edits) {
    if (source.split(before).length !== 2) {
      throw new Error('Pinned Seerr scanner changed; availability patch requires review');
    }
    source = source.replace(before, after);
  }
  return source + '\n' + confirmedPartial.toString() + '\n';
}

module.exports = { confirmedPartial, patch };
if (require.main === module) {
  const target = process.argv[2] || '/app/dist/lib/scanners/baseScanner.js';
  fs.writeFileSync(target, patch(fs.readFileSync(target, 'utf8')));
}
