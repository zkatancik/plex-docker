FROM bitr8/agregarr:2.7.0

# Keep yt-dlp current enough for YouTube extractor changes and security fixes.
ADD --checksum=sha256:e5d57466682cfa9d61e9cf7c8a4f09b00f4a62af37d3bbdc4bcffdf63615feac \
    https://github.com/yt-dlp/yt-dlp/releases/download/2026.06.09/yt-dlp \
    /usr/local/bin/yt-dlp

COPY containers/agregarr/patch-dist.js /tmp/patch-agregarr-dist.js

# Agregarr includes Plex tokens in poster URLs. Preserve the useful poster name
# in logs while ensuring authenticated URLs are never written to Docker logs.
RUN chmod 0755 /usr/local/bin/yt-dlp \
    && sed -i 's@url: originalName ? `${originalName} (${url})` : url,@url: originalName || "[redacted URL]",@g' /app/dist/lib/posterStorage.js \
    && node /tmp/patch-agregarr-dist.js \
    && rm /tmp/patch-agregarr-dist.js \
    && ! grep -F 'originalName ? `${originalName} (${url})` : url' /app/dist/lib/posterStorage.js
