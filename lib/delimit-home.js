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
//
// LED-5658 — the ambient-DELIMIT_HOME trap:
// This machine's own shell profile (~/.bashrc -> ~/.delimit/env) exports a
// concrete DELIMIT_HOME=<realHome>/.delimit into every new shell. Since
// DELIMIT_HOME is (correctly) preferred over $HOME above, the "obvious"
// isolation move — `export HOME=<scratch>` and run a command — silently
// does NOT isolate anything: DELIMIT_HOME is already set to the real store
// from the profile, and merely overriding HOME never touches it. This was
// reproduced live: `delimit remember` under a HOME-only override still
// wrote into the real ~/.delimit/memory. Only clearing the whole
// environment (env -i) worked, because that also drops the ambient
// DELIMIT_HOME. That's an unreasonably sharp edge for a "temp HOME"
// isolation attempt, and the required outcome is that it just works: a
// caller that overrides $HOME is signaling isolation, so an ambient
// DELIMIT_HOME that is merely the DEFAULT for the real (non-overridden)
// home is untrusted and re-derived from the overridden $HOME instead. A
// DELIMIT_HOME that is NOT that default (a genuinely distinct, deliberately
// chosen path) is still honored as explicit intent — this only closes the
// specific "profile leaked the real store" gap, it does not remove the
// ability to point DELIMIT_HOME somewhere custom while HOME is overridden.

const os = require('os');
const path = require('path');

function _realHome() {
    // The OS user database's home directory (getpwuid on POSIX) — ignores
    // $HOME entirely. Used only to detect whether the CALLER has overridden
    // $HOME away from the actual system home (an isolation signal), never
    // as the resolved store location itself.
    try {
        const info = os.userInfo();
        if (info && info.homedir) return info.homedir;
    } catch {
        // os.userInfo() can throw in some sandboxed/containerized setups
        // (no matching passwd entry). Fall back to treating $HOME as
        // authoritative — i.e. skip the mismatch check rather than guess.
    }
    return os.homedir();
}

function delimitHome() {
    const envHome = os.homedir(); // honors $HOME
    const fromEnv = process.env.DELIMIT_HOME || process.env.DELIMIT_NAMESPACE_ROOT;

    if (fromEnv && fromEnv.trim()) {
        const trimmed = fromEnv.trim();
        const realHome = _realHome();
        const homeOverridden = envHome !== realHome;
        if (homeOverridden) {
            const ambientDefault = path.join(realHome, '.delimit');
            if (trimmed !== ambientDefault) {
                // A genuinely distinct DELIMIT_HOME — honor it as explicit
                // intent even while HOME is overridden (e.g. multi-tenant
                // namespacing that deliberately varies the two).
                return trimmed;
            }
            // trimmed === ambientDefault: this is indistinguishable from
            // "the profile set it, and nobody touched it for this
            // invocation" (LED-5658). $HOME was overridden — trust that
            // signal and derive DELIMIT_HOME from it instead.
        } else {
            return trimmed;
        }
    }
    return path.join(envHome, '.delimit');
}

function homeSubpath(...segments) {
    return path.join(delimitHome(), ...segments);
}

module.exports = { delimitHome, homeSubpath };
