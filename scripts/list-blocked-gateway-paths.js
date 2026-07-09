#!/usr/bin/env node
// Single source of truth for proprietary-gating parity.
//
// Emits every `gateway/...` path that package.json "files" NEGATES (i.e.
// blocks from the published npm bundle), one path per line, with the leading
// "!" stripped. Directory negations keep their trailing "/".
//
// Consumed by:
//   - scripts/sync-gateway.sh        → derives the rsync/delete exclusion list
//   - scripts/check-bundle-parity.sh → drift guard (fails if any blocked path
//                                       is present in the committed bundle)
//
// Because BOTH scripts derive from this one extractor, the sync exclusion and
// the parity guard can never drift out of alignment with package.json. Adding
// a new `!gateway/...` negation to package.json automatically extends both.
//
// Note: include patterns (e.g. `gateway/ai/license_core.cpython-*-*.so`, which
// has no leading "!") are intentionally NOT emitted — only exclusions.
'use strict';

const fs = require('fs');
const path = require('path');

const pkgPath = path.join(__dirname, '..', 'package.json');
const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));

const blocked = (pkg.files || [])
  .filter((f) => typeof f === 'string' && f.startsWith('!gateway/'))
  .map((f) => f.slice(1)); // strip leading "!"

process.stdout.write(blocked.join('\n') + (blocked.length ? '\n' : ''));
