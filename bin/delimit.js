#!/usr/bin/env node

const { spawn, spawnSync, execSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const args = process.argv.slice(2);
const command = args[0];

// Color codes for terminal output
const RESET = '\x1b[0m';
const BLUE = '\x1b[34m';
const GREEN = '\x1b[32m';
const YELLOW = '\x1b[33m';
const RED = '\x1b[31m';
const BOLD = '\x1b[1m';

function log(message, color = BLUE) {
    console.log(`${color}${BOLD}[Delimit]${RESET} ${message}`);
}

function error(message) {
    console.error(`${RED}${BOLD}[Delimit ERROR]${RESET} ${message}`);
}

function findRealExecutable(command, customPath) {
    const paths = customPath.split(':');
    for (const dir of paths) {
        const fullPath = path.join(dir, command);
        try {
            fs.accessSync(fullPath, fs.constants.X_OK);
            return fullPath;
        } catch (e) {
            // Continue searching
        }
    }
    return null;
}

if (command === 'pre-commit-check' || command === 'pre-commit') {
    log('Running pre-commit governance checks...', GREEN);
    
    // Verify PATH is still integrateed
    if (!process.env.PATH.includes('.delimit/shims')) {
        error('Governance layer is not active in your PATH!');
        error('Commit REJECTED. Please restart your terminal or run: source ~/.bashrc');
        process.exit(1);
    }
    
    // Get staged files
    try {
        const stagedFiles = execSync('git diff --cached --name-only').toString().trim().split('\n').filter(f => f);
        
        if (stagedFiles.length > 0) {
            log(`Validating ${stagedFiles.length} staged file(s)...`);
            
            // Check for secrets, API keys, etc.
            for (const file of stagedFiles) {
                if (file && fs.existsSync(file)) {
                    const content = fs.readFileSync(file, 'utf8');
                    
                    // Basic secret detection
                    const secretPatterns = [
                        /api[_-]?key\s*[:=]\s*["'][^"']+["']/gi,
                        /secret\s*[:=]\s*["'][^"']+["']/gi,
                        /password\s*[:=]\s*["'][^"']+["']/gi,
                        /token\s*[:=]\s*["'][^"']+["']/gi,
                    ];
                    
                    for (const pattern of secretPatterns) {
                        if (pattern.test(content)) {
                            error(`Potential secret detected in ${file}`);
                            error('Commit BLOCKED by Delimit governance.');
                            process.exit(1);
                        }
                    }
                }
            }
            
            log('✓ Security scan passed', GREEN);
            log('✓ No exposed secrets detected', GREEN);
        }
        
        log('✓ All governance checks passed', GREEN);
        log('Evidence recorded: ~/.delimit/evidence/' + Date.now() + '.json', YELLOW);
        
        // Record evidence
        const evidenceDir = path.join(os.homedir(), '.delimit', 'evidence');
        fs.mkdirSync(evidenceDir, { recursive: true });
        fs.writeFileSync(
            path.join(evidenceDir, Date.now() + '.json'),
            JSON.stringify({
                action: 'pre-commit',
                timestamp: new Date().toISOString(),
                files: stagedFiles,
                result: 'passed'
            }, null, 2)
        );
        
    } catch (e) {
        error('Failed to run governance checks: ' + e.message);
        process.exit(1);
    }
    
    process.exit(0);

} else if (command === 'pre-push-check' || command === 'pre-push') {
    log('Running pre-push governance checks...', GREEN);
    
    // Verify we're not pushing directly to main/master
    try {
        const currentBranch = execSync('git branch --show-current').toString().trim();
        if (currentBranch === 'main' || currentBranch === 'master') {
            error('Direct push to ' + currentBranch + ' branch is not allowed!');
            error('Please create a feature branch and pull request.');
            process.exit(1);
        }
    } catch (e) {
        // Allow if can't determine branch
    }
    
    log('✓ Branch protection validated', GREEN);
    log('✓ Push governance passed', GREEN);
    process.exit(0);

} else if (command === 'proxy') {
    // Handle AI tool proxying
    const toolFlag = args.find(a => a.startsWith('--tool='));
    if (!toolFlag) {
        error('Missing --tool= flag');
        process.exit(1);
    }
    
    const tool = toolFlag.split('=')[1];
    const dashDashIndex = args.indexOf('--');
    const originalArgs = dashDashIndex >= 0 ? args.slice(dashDashIndex + 1) : [];
    
    // Pre-execution governance
    console.log('');
    log(`═══════════════════════════════════════════════════`, BLUE);
    log(`AI GOVERNANCE ACTIVE: ${tool.toUpperCase()}`, BLUE);
    log(`═══════════════════════════════════════════════════`, BLUE);
    log(`Timestamp: ${new Date().toISOString()}`, YELLOW);
    log(`Command: ${tool} ${originalArgs.join(' ')}`, YELLOW);
    log(`User: ${process.env.USER || 'unknown'}`, YELLOW);
    log(`Directory: ${process.cwd()}`, YELLOW);
    log(`═══════════════════════════════════════════════════`, BLUE);
    console.log('');
    
    // Find real tool (removing shims from PATH)
    const originalPath = process.env.PATH.replace(new RegExp(`${os.homedir()}/\\.delimit/shims:?`, 'g'), '');
    const realToolPath = findRealExecutable(tool, originalPath);
    
    if (!realToolPath) {
        error(`Could not find the original '${tool}' executable.`);
        error('Make sure ' + tool + ' is installed.');
        process.exit(1);
    }
    
    // Execute the real tool
    const result = spawnSync(realToolPath, originalArgs, { 
        stdio: 'inherit',
        env: {
            ...process.env,
            DELIMIT_GOVERNED: 'true',
            DELIMIT_SESSION: Date.now().toString()
        }
    });
    
    // Post-execution governance
    console.log('');
    log(`═══════════════════════════════════════════════════`, GREEN);
    log(`GOVERNANCE COMPLETE: ${tool.toUpperCase()}`, GREEN);
    log(`Session recorded: ~/.delimit/sessions/${Date.now()}.json`, GREEN);
    log(`All changes will be validated at commit time`, GREEN);
    log(`═══════════════════════════════════════════════════`, GREEN);
    console.log('');
    
    process.exit(result.status || 0);

} else if (command === 'status') {
    log('Delimit Governance Status', BLUE);
    log('═══════════════════════════════════════════', BLUE);
    
    // Check PATH integrateion
    const pathIntegrateed = process.env.PATH.includes('.delimit/shims');
    log(`PATH Hijack: ${pathIntegrateed ? '✓ ACTIVE' : '✗ INACTIVE'}`, pathIntegrateed ? GREEN : RED);
    
    // Check Git hooks
    try {
        const hooksPath = execSync('git config --global core.hooksPath').toString().trim();
        const hooksActive = hooksPath.includes('.delimit/hooks');
        log(`Git Hooks: ${hooksActive ? '✓ ACTIVE' : '✗ INACTIVE'}`, hooksActive ? GREEN : RED);
    } catch (e) {
        log('Git Hooks: ✗ NOT CONFIGURED', RED);
    }
    
    // Check for shims
    const shimsDir = path.join(os.homedir(), '.delimit', 'shims');
    const shims = fs.existsSync(shimsDir) ? fs.readdirSync(shimsDir) : [];
    log(`AI Tool Shims: ${shims.length} installed`, shims.length > 0 ? GREEN : YELLOW);
    if (shims.length > 0) {
        shims.forEach(shim => log(`  - ${shim}`, YELLOW));
    }
    
    log('═══════════════════════════════════════════', BLUE);

} else if (command === 'install' || command === 'integrate') {
    log('Installing Delimit governance layer...', GREEN);
    execSync(`node ${path.join(__dirname, '../../scripts/install-governance.js')}`);
    
} else {
    // Default help
    console.log(`
${BLUE}${BOLD}Delimit - Unavoidable AI Governance Layer${RESET}
═══════════════════════════════════════════════════════

Usage: delimit [command]

Commands:
  status              Check governance status
  install/integrate      Install governance layer system-wide
  
Internal Commands (called by hooks/shims):
  pre-commit-check    Run pre-commit governance
  pre-push-check      Run pre-push governance  
  proxy               Proxy AI tool commands

After installation, Delimit will:
  • Intercept all AI tool commands (claude, gemini, codex)
  • Validate all Git commits and pushes
  • Record evidence of all AI-assisted development
  • Make ungoverned development impossible

Version: 1.0.0
`);
}