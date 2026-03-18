#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

const HOME_DIR = os.homedir();
const DELIMIT_HOME = path.join(HOME_DIR, '.delimit');
const SHIM_DIR = path.join(DELIMIT_HOME, 'shims');
const HOOKS_DIR = path.join(DELIMIT_HOME, 'hooks');
const BIN_DIR = path.join(DELIMIT_HOME, 'bin');

console.log('\n🔵 Installing Delimit Governance Layer...');
console.log('⚠️  WARNING: This will modify your system permanently.\n');

try {
    // 1. Create directory structure
    [DELIMIT_HOME, SHIM_DIR, HOOKS_DIR, BIN_DIR].forEach(dir => {
        fs.mkdirSync(dir, { recursive: true });
    });
    console.log('✓ Created ~/.delimit directory structure');

    // 2. Copy the main Delimit CLI
    const cliSource = path.join(__dirname, '..', 'bin', 'delimit.js');
    const cliDest = path.join(BIN_DIR, 'delimit');
    fs.copyFileSync(cliSource, cliDest);
    fs.chmodSync(cliDest, '755');
    console.log('✓ Installed Delimit CLI');

    // 3. Install global Git hooks
    const preCommitHook = `#!/bin/sh
# Delimit Governance Hook - Pre-commit
${cliDest} pre-commit-check`;

    const prePushHook = `#!/bin/sh
# Delimit Governance Hook - Pre-push  
${cliDest} pre-push-check`;

    fs.writeFileSync(path.join(HOOKS_DIR, 'pre-commit'), preCommitHook);
    fs.writeFileSync(path.join(HOOKS_DIR, 'pre-push'), prePushHook);
    fs.chmodSync(path.join(HOOKS_DIR, 'pre-commit'), '755');
    fs.chmodSync(path.join(HOOKS_DIR, 'pre-push'), '755');
    
    execSync(`git config --global core.hooksPath ${HOOKS_DIR}`);
    console.log('✓ Installed global Git hooks');

    // 4. Create AI tool shims
    const aiTools = ['claude', 'gemini', 'codex', 'copilot', 'gh', 'openai', 'anthropic'];
    aiTools.forEach(tool => {
        const shimContent = `#!/bin/sh
# Delimit Governance Shim for ${tool}
exec ${cliDest} proxy --tool=${tool} -- "$@"`;
        
        const shimPath = path.join(SHIM_DIR, tool);
        fs.writeFileSync(shimPath, shimContent);
        fs.chmodSync(shimPath, '755');
    });
    console.log(`✓ Created ${aiTools.length} AI tool shims`);

    // 5. Inject into shell profiles
    const shellProfiles = [
        '.bashrc',
        '.zshrc', 
        '.profile',
        '.bash_profile'
    ].map(f => path.join(HOME_DIR, f));

    const pathInjection = `
# Delimit Governance Layer - DO NOT REMOVE
export PATH="${SHIM_DIR}:$PATH"
export DELIMIT_ACTIVE=true

# Show governance status on shell start
if [ -t 1 ]; then
  echo -e "\\033[34m\\033[1m[Delimit]\\033[0m Governance active. All AI tools and Git operations are monitored."
fi
`;

    let injected = false;
    shellProfiles.forEach(profilePath => {
        if (fs.existsSync(profilePath)) {
            const content = fs.readFileSync(profilePath, 'utf8');
            if (!content.includes('Delimit Governance Layer')) {
                fs.appendFileSync(profilePath, pathInjection);
                console.log(`✓ Injected into ${path.basename(profilePath)}`);
                injected = true;
            }
        }
    });

    if (!injected) {
        // Create a .profile if nothing exists
        const profilePath = path.join(HOME_DIR, '.profile');
        fs.writeFileSync(profilePath, pathInjection);
        console.log('✓ Created .profile with governance');
    }

    // 6. Create global command link
    try {
        const globalBin = '/usr/local/bin/delimit';
        if (fs.existsSync('/usr/local/bin')) {
            if (fs.existsSync(globalBin)) {
                fs.unlinkSync(globalBin);
            }
            fs.symlinkSync(cliDest, globalBin);
            console.log('✓ Created global delimit command');
        }
    } catch (e) {
        // Ignore if can't create global link
    }

    console.log('\n' + '═'.repeat(60));
    console.log('🟢 DELIMIT GOVERNANCE LAYER INSTALLED SUCCESSFULLY');
    console.log('═'.repeat(60));
    console.log('\n⚡ IMPORTANT: Restart your terminal or run:');
    console.log('   source ~/.bashrc (or ~/.zshrc)\n');
    console.log('📊 Check status with: delimit status');
    console.log('📖 Documentation: https://delimit.ai\n');
    console.log('⚠️  WARNING: Governance is now mandatory.');
    console.log('   All AI tools and Git operations are monitored.\n');

} catch (error) {
    console.error('\n❌ Installation failed:', error.message);
    console.error('\nTry installing globally with sudo:');
    console.error('   sudo npm install -g delimit\n');
    process.exit(1);
}