const fs = require('fs');
const path = require('path');

const distRoot = process.env.AGREGARR_DIST_ROOT || '/app/dist';

function patchFile(relativePath, edits) {
  const filePath = path.join(distRoot, relativePath);
  let source = fs.readFileSync(filePath, 'utf8');

  for (const { name, before, after } of edits) {
    const occurrences = source.split(before).length - 1;
    if (occurrences !== 1) {
      throw new Error(
        `${relativePath}: expected exactly one ${name} target, found ${occurrences}`
      );
    }
    source = source.replace(before, after);
  }

  fs.writeFileSync(filePath, source);
}

patchFile('api/plexapi.js', [
  {
    name: 'bulk collection add fallback',
    before: `        catch (error) {
            failed = itemsToActuallyAdd.length;
            logger_1.default.error(\`Error adding \${itemsToActuallyAdd.length} items to collection \${collectionRatingKey}\`, {
                label: 'Plex API',
                error: error instanceof Error ? error.message : error,
                itemCount: itemsToActuallyAdd.length,
                uri: uri.length > 200 ? uri.substring(0, 200) + '...' : uri,
            });
        }`,
    after: `        catch (error) {
            // A placeholder can disappear while Plex is scanning. One stale key
            // makes the bulk request fail, so retry live keys individually and
            // preserve every valid collection member.
            logger_1.default.warn(\`Bulk add failed for collection \${collectionRatingKey}; retrying items individually\`, {
                label: 'Plex API',
                error: error instanceof Error ? error.message : error,
                itemCount: itemsToActuallyAdd.length,
            });
            for (const ratingKey of itemsToActuallyAdd) {
                const itemUri = \`server://\${machineId}/com.plexapp.plugins.library/library/metadata/\${ratingKey}\`;
                const itemAddUrl = \`/library/collections/\${collectionRatingKey}/items?uri=\${encodeURIComponent(itemUri)}\`;
                try {
                    await this.safePutQuery(hasEpisodes
                        ? \`\${itemAddUrl}&type=4\`
                        : itemAddUrl);
                    successful++;
                }
                catch (itemError) {
                    failed++;
                    logger_1.default.warn(\`Skipping unavailable item \${ratingKey} while updating collection \${collectionRatingKey}\`, {
                        label: 'Plex API',
                        error: itemError instanceof Error ? itemError.message : itemError,
                    });
                }
            }
            if (successful === 0) {
                logger_1.default.error(\`No items could be added to collection \${collectionRatingKey}\`, {
                    label: 'Plex API',
                    attempted: itemsToActuallyAdd.length,
                    failed,
                });
            }
        }`,
  },
]);

patchFile('lib/collections/core/BaseCollectionSync.js', [
  {
    name: 'collection update result validation',
    before: `                        const updateResult = await plexClient.updateCollectionContents(collectionRatingKey, plexItems);
                        // Label items that fell out of the collection as stale`,
    after: `                        const updateResult = await plexClient.updateCollectionContents(collectionRatingKey, plexItems);
                        // Do not report a successful update when a non-empty desired
                        // collection remained empty after Plex rejected every add.
                        if (plexItems.length > 0 && updateResult.added === 0) {
                            const resultingItems = await plexClient.getCollectionItems(collectionRatingKey);
                            if (resultingItems.length === 0) {
                                throw new Error(\`Collection update left "\${collectionName}" empty: \${updateResult.errors.join('; ') || 'no items were accepted by Plex'}\`);
                            }
                        }
                        if (updateResult.errors.length > 0) {
                            logger_1.default.warn(\`Collection "\${collectionName}" was updated with recoverable item errors\`, {
                                label: 'Collection Update',
                                collectionRatingKey,
                                errors: updateResult.errors,
                                added: updateResult.added,
                            });
                        }
                        // Label items that fell out of the collection as stale`,
  },
  {
    name: 'no-op collection title guard',
    before: `        if (collectionName) {
            try {
                await plexClient.updateCollectionTitle(collectionRatingKey, collectionName, libraryKey);`,
    after: `        if (collectionName &&
            (await plexClient.getCollectionMetadataSafe(collectionRatingKey))?.title !== collectionName) {
            try {
                await plexClient.updateCollectionTitle(collectionRatingKey, collectionName, libraryKey);`,
  },
]);

patchFile('lib/overlays/OverlayLibraryService.js', [
  {
    name: 'IMDb to TMDB fallback',
    before: `            // Check if this is a placeholder (async version with API call for suspicious items)`,
    after: `            // Plex occasionally exposes only an IMDb GUID. Resolve it through
            // TMDB so release, Arr matching, and poster overlays still work.
            if (!tmdbId && item.Guid && Array.isArray(item.Guid)) {
                const imdbGuid = item.Guid.find((g) => g.id?.startsWith('imdb://'));
                const imdbId = imdbGuid?.id?.replace('imdb://', '');
                if (imdbId) {
                    try {
                        const TheMovieDb = (await Promise.resolve().then(() => __importStar(require('../../api/themoviedb')))).default;
                        const externalMatches = await new TheMovieDb().getByExternalId({
                            externalId: imdbId,
                            type: 'imdb',
                        });
                        const matches = actualMediaType === 'movie'
                            ? externalMatches.movie_results
                            : externalMatches.tv_results;
                        if (matches?.[0]?.id) {
                            tmdbId = Number(matches[0].id);
                            logger_1.default.debug('Resolved missing TMDB ID from IMDb GUID', {
                                label: 'OverlayLibrary',
                                itemTitle: item.title,
                                ratingKey: item.ratingKey,
                                tmdbId,
                            });
                        }
                    }
                    catch (resolveError) {
                        logger_1.default.warn('Could not resolve TMDB ID from IMDb GUID', {
                            label: 'OverlayLibrary',
                            itemTitle: item.title,
                            ratingKey: item.ratingKey,
                            error: resolveError instanceof Error ? resolveError.message : String(resolveError),
                        });
                    }
                }
            }
            // Check if this is a placeholder (async version with API call for suspicious items)`,
  },
  {
    name: 'missing base poster handling',
    before: `            catch (error) {
                // Re-throw to let caller track this as a failure
                // Previously this was silently returning, causing failed items to be counted as success
                throw new Error(\`Failed to get base poster for "\${item.title}": \${error instanceof Error ? error.message : String(error)}\`);
            }
            const posterBuffer = basePosterResult.posterBuffer;`,
    after: `            catch (error) {
                const posterError = error instanceof Error ? error.message : String(error);
                // Missing third-party artwork is not an application failure. Keep
                // the existing Plex poster intact and retry on a future sync.
                if (posterError === 'No TMDB poster available' ||
                    posterError === 'No TMDB ID found for item') {
                    logger_1.default.warn('Skipping overlay because no safe base poster is available', {
                        label: 'OverlayLibrary',
                        itemTitle: item.title,
                        ratingKey: item.ratingKey,
                        reason: posterError,
                    });
                    return { skipped: true };
                }
                throw new Error(\`Failed to get base poster for "\${item.title}": \${posterError}\`);
            }
            const posterBuffer = basePosterResult.posterBuffer;`,
  },
]);

patchFile('lib/collections/sources/trakt.js', [
  {
    name: 'preserve Trakt IMDb IDs for Plex matching',
    before: `                traktLookups.push({
                    tmdbId,
                    showTmdbId,`,
    after: `                traktLookups.push({
                    tmdbId,
                    imdbId: mediaItem.ids.imdb,
                    showTmdbId,`,
  },
  {
    name: 'preserve IMDb ID on missing Trakt items',
    before: `                    missingItems.push({
                        tmdbId: lookup.tmdbId,
                        mediaType: lookup.mediaType,`,
    after: `                    missingItems.push({
                        tmdbId: lookup.tmdbId,
                        imdbId: lookup.imdbId,
                        mediaType: lookup.mediaType,`,
  },
  {
    name: 'preserve IMDb ID for duplicate detection',
    before: `            const missingLookups = missingItems.map((item) => ({
                tmdbId: item.tmdbId,
                mediaType: item.mediaType,`,
    after: `            const missingLookups = missingItems.map((item) => ({
                tmdbId: item.tmdbId,
                imdbId: item.imdbId,
                mediaType: item.mediaType,`,
  },
]);

patchFile('lib/collections/core/CollectionUtilities.js', [
  {
    name: 'library cache year preservation',
    before: `                ratingKey: item.ratingKey,
                title: item.title,
                addedAt: item.addedAt,`,
    after: `                ratingKey: item.ratingKey,
                title: item.title,
                year: item.year,
                addedAt: item.addedAt,`,
  },
  {
    name: 'IMDb GUID extractor',
    before: `exports.extractTvdbIdFromGuids = extractTvdbIdFromGuids;
/**
 * Extract TMDB ID from Plex GUID array.`,
    after: `exports.extractTvdbIdFromGuids = extractTvdbIdFromGuids;
function extractImdbIdFromGuids(guids) {
    if (!guids || guids.length === 0) {
        return undefined;
    }
    const imdbGuid = guids.find((guid) => guid.id && guid.id.startsWith('imdb://'));
    return imdbGuid?.id ? imdbGuid.id.replace('imdb://', '') : undefined;
}
/**
 * Extract TMDB ID from Plex GUID array.`,
  },
  {
    name: 'IMDb movie matching fallback',
    before: `                        const tmdbId = extractTmdbIdFromGuids(item.Guid);
                        if (tmdbId) {
                            foundTmdbIds.push(tmdbId);
                            const lookup = movieLookups.find((l) => l.tmdbId === tmdbId);`,
    after: `                        const tmdbId = extractTmdbIdFromGuids(item.Guid);
                        if (!tmdbId) {
                            const imdbId = extractImdbIdFromGuids(item.Guid);
                            const imdbLookup = imdbId
                                ? movieLookups.find((lookup) => lookup.imdbId === imdbId)
                                : undefined;
                            const titleYearLookup = movieLookups.find((lookup) => lookup.title?.trim().toLowerCase() === item.title?.trim().toLowerCase() &&
                                lookup.year && item.year && Number(lookup.year) === Number(item.year));
                            const fallbackLookup = imdbLookup || titleYearLookup;
                            if (fallbackLookup) {
                                const key = \`\${fallbackLookup.tmdbId}-movie\`;
                                const tvdbId = extractTvdbIdFromGuids(item.Guid);
                                results.set(key, {
                                    ratingKey: item.ratingKey,
                                    title: item.title,
                                    libraryKey: library.key,
                                    addedAt: item.addedAt,
                                    releaseDate: item.releaseDate,
                                    tvdbId,
                                });
                                logger_1.default.debug('Matched Plex movie without a TMDB GUID', {
                                    label: 'Plex Search (Metadata Fallback)',
                                    ratingKey: item.ratingKey,
                                    tmdbId: fallbackLookup.tmdbId,
                                    matchMethod: imdbLookup ? 'imdb' : 'title-year',
                                });
                            }
                        }
                        if (tmdbId) {
                            foundTmdbIds.push(tmdbId);
                            const lookup = movieLookups.find((l) => l.tmdbId === tmdbId);`,
  },
  {
    name: 'IMDb TV matching fallback',
    before: `                        const tmdbId = extractTmdbIdFromGuids(item.Guid);
                        if (tmdbId) {
                            foundTmdbIds.push(tmdbId);
                            // First try regular show lookup`,
    after: `                        const tmdbId = extractTmdbIdFromGuids(item.Guid);
                        if (!tmdbId) {
                            const imdbId = extractImdbIdFromGuids(item.Guid);
                            const imdbLookup = imdbId
                                ? tvLookups.find((lookup) => lookup.imdbId === imdbId && !lookup.episodeInfo)
                                : undefined;
                            const titleYearLookup = tvLookups.find((lookup) => !lookup.episodeInfo &&
                                lookup.title?.trim().toLowerCase() === item.title?.trim().toLowerCase() &&
                                lookup.year && item.year && Number(lookup.year) === Number(item.year));
                            const fallbackLookup = imdbLookup || titleYearLookup;
                            if (fallbackLookup) {
                                const key = \`\${fallbackLookup.tmdbId}-tv\`;
                                const tvdbId = extractTvdbIdFromGuids(item.Guid);
                                results.set(key, {
                                    ratingKey: item.ratingKey,
                                    title: item.title,
                                    libraryKey: library.key,
                                    addedAt: item.addedAt,
                                    releaseDate: item.releaseDate,
                                    tvdbId,
                                });
                                logger_1.default.debug('Matched Plex show without a TMDB GUID', {
                                    label: 'Plex Search (Metadata Fallback)',
                                    ratingKey: item.ratingKey,
                                    tmdbId: fallbackLookup.tmdbId,
                                    matchMethod: imdbLookup ? 'imdb' : 'title-year',
                                });
                            }
                        }
                        if (tmdbId) {
                            foundTmdbIds.push(tmdbId);
                            // First try regular show lookup`,
  },
]);

patchFile('lib/posterStorage.js', [
  {
    name: 'expected Plex poster 404 handling',
    before: `    catch (error) {
        logger_1.default.error('Failed to download poster from URL', {
            url: originalName || "[redacted URL]",
            error: error instanceof Error ? error.message : String(error),
        });
        return null;
    }
}
exports.downloadAndSavePoster = downloadAndSavePoster;`,
    after: `    catch (error) {
        const status = error?.response?.status;
        if (status === 404) {
            // Some Plex-generated hubs have no downloadable custom artwork.
            // Preserve the existing hub and treat that as an expected no-poster case.
            logger_1.default.debug('Poster URL has no artwork; leaving existing Plex poster unchanged', {
                url: originalName || "[redacted URL]",
                status,
            });
        }
        else {
            logger_1.default.error('Failed to download poster from URL', {
                url: originalName || "[redacted URL]",
                error: error instanceof Error ? error.message : String(error),
            });
        }
        return null;
    }
}
exports.downloadAndSavePoster = downloadAndSavePoster;`,
  },
]);

patchFile('lib/placeholders/trailerDownload.js', [
  {
    name: 'Apple TV direct-play trailer format',
    before: `'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]'`,
    after: `'bestvideo[height<=1080][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a][acodec^=mp4a]/best[height<=1080][ext=mp4][vcodec^=avc1][acodec^=mp4a]'`,
  },
]);

patchFile('lib/placeholders/placeholderManager.js', [
  {
    name: 'trailer checksum helper',
    before: `const fs_1 = require("fs");
const promises_1 = __importDefault(require("fs/promises"));
const path_1 = __importDefault(require("path"));`,
    after: `const fs_1 = require("fs");
const promises_1 = __importDefault(require("fs/promises"));
const path_1 = __importDefault(require("path"));
const crypto_1 = require("crypto");
function sha256File(filePath) {
    return new Promise((resolve, reject) => {
        const hash = (0, crypto_1.createHash)('sha256');
        const stream = (0, fs_1.createReadStream)(filePath);
        stream.on('error', reject);
        stream.on('data', (chunk) => hash.update(chunk));
        stream.on('end', () => resolve(hash.digest('hex')));
    });
}
async function copyTrailerVerified(sourcePath, destinationPath) {
    await promises_1.default.copyFile(sourcePath, destinationPath, fs_1.constants.COPYFILE_EXCL);
    try {
        // Force the bind-mount write through before reading it back. This makes
        // Docker/ZFS transport corruption visible before Plex can scan the file.
        const destinationHandle = await promises_1.default.open(destinationPath, 'r');
        try {
            await destinationHandle.sync();
        }
        finally {
            await destinationHandle.close();
        }
        const [sourceHash, destinationHash] = await Promise.all([
            sha256File(sourcePath),
            sha256File(destinationPath),
        ]);
        if (sourceHash !== destinationHash) {
            const error = new Error('Trailer checksum mismatch after destination copy');
            error.code = 'EINTEGRITY';
            throw error;
        }
    }
    catch (error) {
        try {
            await promises_1.default.unlink(destinationPath);
        }
        catch {
            // Best effort: the original integrity/copy error is authoritative.
        }
        throw error;
    }
}`,
  },
  {
    name: 'verified movie trailer copy',
    before: `    await promises_1.default.copyFile(trailerPath, destinationPath, fs_1.constants.COPYFILE_EXCL);
    // Write the .comingsoon marker (atomic) for identification. Mirrors the TV`,
    after: `    await copyTrailerVerified(trailerPath, destinationPath);
    // Write the .comingsoon marker (atomic) for identification. Mirrors the TV`,
  },
  {
    name: 'verified TV trailer copy',
    before: `    // Copy trailer file (COPYFILE_EXCL: never clobber an existing file).
    await promises_1.default.copyFile(trailerPath, destinationPath, fs_1.constants.COPYFILE_EXCL);
    // Clean up temporary trailer file`,
    after: `    // Copy trailer file and prove its bind-mount bytes before Plex sees it.
    await copyTrailerVerified(trailerPath, destinationPath);
    // Clean up temporary trailer file`,
  },
]);

console.log('Applied Agregarr runtime hardening patches.');
