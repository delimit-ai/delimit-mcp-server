const path = require('path');

function claudeMcpAddArgs(delimitHome, python, server) {
    const serverDir = path.join(delimitHome, 'server');
    return ['mcp', 'add', '--scope', 'user', 'delimit', '-e',
        `PYTHONPATH=${serverDir}:${path.join(serverDir, 'ai')}`,
        '--', python, server];
}

function displayClaudeCommand(args) {
    const quote = value => /^[A-Za-z0-9_./:=+-]+$/.test(value)
        ? value : `'${value.replace(/'/g, "'\\''")}'`;
    return ['claude', ...args].map(quote).join(' ');
}

module.exports = { claudeMcpAddArgs, displayClaudeCommand };
