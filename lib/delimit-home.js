// lib/delimit-home.js
//
// LED-1188: single source of truth for resolving the Delimit private-state
// directory (~/.delimit by default). Replaces ~37 hardcoded sites across
// bin/delimit-setup.js, bin/delimit-cli.js, and gateway adapters.
//
// Resolution order:
//   1. $DELIMIT_HOME            (preferred — explicit, easy to reason about)
//   2. $DELIMIT_NAMESPACE_ROOT  (gateway-compat — see continuity.py:454)
//   3. <homedir>/.delimit       (default)
//
// USAGE
//   const { delimitHome, homeSubpath } = require('../lib/delimit-home');
//   const ledger = path.join(delimitHome(), 'ledger');
//   const ledger = homeSubpath('ledger');                  // shorthand
//
// Both helpers re-resolve on every call so tests can mutate process.env
// between calls without module-cache invalidation.

const os = require('os');
const path = require('path');

function delimitHome() {
    const fromEnv = process.env.DELIMIT_HOME || process.env.DELIMIT_NAMESPACE_ROOT;
    if (fromEnv && fromEnv.trim()) {
        return fromEnv;
    }
    return path.join(os.homedir(), '.delimit');
}

function homeSubpath(...segments) {
    return path.join(delimitHome(), ...segments);
}

module.exports = { delimitHome, homeSubpath };
