const fs = require('fs');
const path = require('path');
const chalk = require('chalk');

function generateTimeline(venturePath) {
    const homedir = require('os').homedir();
    const delimitHome = path.join(homedir, '.delimit');
    const ledgerDir = path.join(delimitHome, 'ledger');
    
    const events = [];

    // Read all ledger files
    const files = ['operations.jsonl', 'strategy.jsonl'];
    for (const file of files) {
        const filePath = path.join(ledgerDir, file);
        if (fs.existsSync(filePath)) {
            const lines = fs.readFileSync(filePath, 'utf-8').trim().split('\n');
            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    const event = JSON.parse(line);
                    events.push(event);
                } catch (e) {}
            }
        }
    }

    // Sort by timestamp (fallback to 0)
    events.sort((a, b) => new Date(a.created_at || a.ts || 0) - new Date(b.created_at || b.ts || 0));

    if (events.length === 0) {
        return "No history found in ledger.";
    }

    let output = chalk.bold.blue("\n  Delimit Venture Timeline — civilization-style retrospective\n\n");
    
    let lastDate = "";
    for (const e of events) {
        const ts = e.created_at || e.ts || new Date(0).toISOString();
        const date = ts.split('T')[0];
        if (date !== lastDate) {
            output += chalk.bold.white(`\n  --- ${date} ---\n`);
            lastDate = date;
        }

        const time = ts.split('T')[1]?.slice(0, 5) || "??:??";
        const priority = e.priority === 'P0' ? chalk.red('P0') : e.priority === 'P1' ? chalk.yellow('P1') : chalk.gray(e.priority || 'n/a');
        const type = e.type === 'strategy' ? chalk.magenta('STR') : chalk.cyan('OPS');
        
        output += `  ${chalk.gray(time)} [${type}] [${priority}] ${chalk.white(e.title || e.message || 'Untitled Event')}\n`;
        if (e.status === 'done' || e.status === 'completed') {
            output += `         ${chalk.green('✓ COMPLETED')}\n`;
        }
    }

    output += chalk.dim(`\n  Total events: ${events.length}\n`);
    return output;
}

module.exports = { generateTimeline };
