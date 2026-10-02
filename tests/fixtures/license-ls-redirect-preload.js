'use strict';
/**
 * TEST-ONLY preload (node -r) for tests/license-activation.test.js.
 * Not shipped: package.json "files" does not include tests/.
 *
 * Redirects the CLI's Lemon Squeezy validate call to a local fake server
 * (_TEST_FAKE_LS_URL) and blocks every other axios request, so the CLI
 * under test never reaches the real Lemon Squeezy API or delimit.ai.
 * With _TEST_FAKE_LS_URL unset, the LS call fails like a network error.
 */
const path = require('path');
const axios = require(require.resolve('axios', { paths: [path.resolve(__dirname, '..', '..')] }));

const LS_URL = 'https://api.lemonsqueezy.com/v1/licenses/validate';
const realPost = axios.post.bind(axios);

function blocked(url) {
    const err = new Error(`network blocked in test: ${url}`);
    err.code = 'ECONNREFUSED';
    return Promise.reject(err);
}

axios.post = function patchedPost(url, data, config) {
    if (url === LS_URL && process.env._TEST_FAKE_LS_URL) {
        return realPost(process.env._TEST_FAKE_LS_URL, data, config);
    }
    return blocked(url);
};
axios.get = function patchedGet(url) {
    return blocked(url);
};
