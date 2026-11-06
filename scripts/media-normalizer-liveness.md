# Scheduled media work and memory deferral

Each native FFmpeg admission attempt waits at most 60 seconds. The existing
normalizer and shared media-mutation locks still protect the transaction. A
capacity deferral or interrupted subprocess ends the current poll with exit 75;
it does not become a permanent media-quality failure. This limits a denied wait,
not the total duration of a poll or a successful encode.

Before processing, a poll saves its unfinished intent. It preserves the import
watermark until completion and checkpoints completed files with device, inode,
size, modification time and change time. A checkpoint requires matching pre/post
stamps and the persisted processing result's identity. Changed files are
revisited. A normalized replacement may receive one safe compatibility
reinspection before gaining the checkpoint shortcut. Definite decoder failure
stops that file's remaining sample decodes; later files can still progress.

The existing 04:00 and 16:00 daily schedule is retained. A separate native
LaunchAgent invokes `retry-deferred` every 15 minutes. With no unfinished poll or
eligible interrupted/capacity-deferred file, it exits without service calls or a
media lock. Otherwise it resumes the ordinary poll under the existing process
and media locks. The runtime retry agent copies the original agent's admission
environment, with its wait set to 60 seconds. No notification or Codex chat is
needed to retry. The automatic retry command refuses missing admission config.

This continuation contract covers scheduled polls. An explicit `normalize
--season` invocation returns 75 on its first transient deferral; it does not save
the remainder of that manual season selection as a separate automatic job.
Native work still requires its ordinary eligible pressure route. These changes
do not qualify VideoToolbox encoding under warning pressure, enforce a native
RSS cap, or preserve a coordinator request's physical queue age after it expires.

Validation: 78 tests, including real temporary state/lock continuation, unchanged
completed A followed by deferred B and unvisited C, file replacement and same-size
edits around checkpoints, idle retry, missing admission, and reserved retry exit
codes. No production media was processed during installation. The first loaded
retry pass exited 0 and preserved the production state bytes.
