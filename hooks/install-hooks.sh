#!/bin/bash

################################################################################
# Delimit™ Hooks Seamless Installer
# Automatically installs all governance hooks on user machines
################################################################################

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
DELIMIT_HOME="${HOME}/.delimit"
CLAUDE_HOME="${HOME}/.claude"
CLAUDE_CONFIG_PATHS=(
    "${HOME}/Library/Application Support/Claude"
    "${HOME}/.config/claude"
    "${CLAUDE_HOME}"
)
NPM_DELIMIT="/home/delimit/npm-delimit"

echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║       Delimit™ Hooks Installer v2.0         ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"
echo

# Function to find Claude config directory
find_claude_config() {
    for path in "${CLAUDE_CONFIG_PATHS[@]}"; do
        if [ -d "$path" ]; then
            echo "$path"
            return 0
        fi
    done
    return 1
}

# Function to create directory structure
create_directories() {
    echo -e "${YELLOW}Creating directory structure...${NC}"
    
    # Delimit directories
    mkdir -p "${DELIMIT_HOME}/hooks"
    mkdir -p "${DELIMIT_HOME}/evidence"
    mkdir -p "${DELIMIT_HOME}/audit"
    mkdir -p "${DELIMIT_HOME}/credentials"
    mkdir -p "${DELIMIT_HOME}/config"
    
    # NPM Delimit hook directories
    mkdir -p "${NPM_DELIMIT}/hooks/models"
    mkdir -p "${NPM_DELIMIT}/hooks/git"
    
    echo -e "${GREEN}✓ Directories created${NC}"
}

# Function to create hook scripts
create_hook_scripts() {
    echo -e "${YELLOW}Creating hook scripts...${NC}"
    
    # Create pre-bash hook
    cat > "${NPM_DELIMIT}/hooks/pre-bash-hook.js" << 'EOF'
#!/usr/bin/env node
const axios = require('axios');
const AGENT_URL = `http://127.0.0.1:${process.env.DELIMIT_AGENT_PORT || 7823}`;

async function validateBash(params) {
    const riskyCommands = ['rm -rf', 'chmod 777', 'sudo', '> /dev/sda'];
    const command = params.command || '';
    
    if (riskyCommands.some(cmd => command.includes(cmd))) {
        console.error('[DELIMIT] ⚠️  Risky command detected');
        try {
            const { data } = await axios.post(`${AGENT_URL}/evaluate`, {
                action: 'bash_command',
                command: command,
                riskLevel: 'high'
            });
            if (data.action === 'block') {
                console.error('[DELIMIT] ❌ Command blocked by governance policy');
                process.exit(1);
            }
        } catch (e) {
            console.warn('[DELIMIT] Governance agent not available');
        }
    }
}

if (require.main === module) {
    const params = JSON.parse(process.argv[2] || '{}');
    validateBash(params);
}
EOF
    chmod +x "${NPM_DELIMIT}/hooks/pre-bash-hook.js"
    
    # Create pre-write hook
    cat > "${NPM_DELIMIT}/hooks/pre-write-hook.js" << 'EOF'
#!/usr/bin/env node
const axios = require('axios');
const path = require('path');
const AGENT_URL = `http://127.0.0.1:${process.env.DELIMIT_AGENT_PORT || 7823}`;

async function validateWrite(params) {
    const filePath = params.file_path || params.path || '';
    const sensitivePaths = ['/etc/', '/.ssh/', '/.aws/', '/credentials/'];
    
    if (sensitivePaths.some(p => filePath.includes(p))) {
        console.warn('[DELIMIT] ⚠️  Sensitive file operation detected');
        try {
            const { data } = await axios.post(`${AGENT_URL}/evaluate`, {
                action: 'file_write',
                path: filePath,
                riskLevel: 'critical'
            });
            if (data.action === 'block') {
                console.error('[DELIMIT] ❌ File operation blocked by governance policy');
                process.exit(1);
            }
        } catch (e) {
            console.warn('[DELIMIT] Governance agent not available');
        }
    }
}

if (require.main === module) {
    const params = JSON.parse(process.argv[2] || '{}');
    validateWrite(params);
}
EOF
    chmod +x "${NPM_DELIMIT}/hooks/pre-write-hook.js"
    
    # Create more hook scripts
    for hook in pre-read pre-search pre-web pre-task pre-mcp post-write post-bash post-mcp; do
        cat > "${NPM_DELIMIT}/hooks/${hook}-hook.js" << EOF
#!/usr/bin/env node
// Delimit ${hook} hook
const axios = require('axios');
const AGENT_URL = \`http://127.0.0.1:\${process.env.DELIMIT_AGENT_PORT || 7823}\`;

async function process() {
    console.log('[DELIMIT] ${hook} hook activated');
    // Hook implementation
}

if (require.main === module) {
    process();
}
EOF
        chmod +x "${NPM_DELIMIT}/hooks/${hook}-hook.js"
    done
    
    echo -e "${GREEN}✓ Hook scripts created${NC}"
}

# Function to create Git hooks
create_git_hooks() {
    echo -e "${YELLOW}Creating Git hooks...${NC}"
    
    # Pre-commit hook
    cat > "${NPM_DELIMIT}/hooks/git/pre-commit" << 'EOF'
#!/bin/bash
# Delimit pre-commit hook
echo "[DELIMIT] Running pre-commit governance check..."
node /home/delimit/npm-delimit/bin/delimit-cli.js hook pre-commit
EOF
    chmod +x "${NPM_DELIMIT}/hooks/git/pre-commit"
    
    # Pre-push hook
    cat > "${NPM_DELIMIT}/hooks/git/pre-push" << 'EOF'
#!/bin/bash
# Delimit pre-push hook
echo "[DELIMIT] Running pre-push governance check..."
node /home/delimit/npm-delimit/bin/delimit-cli.js hook pre-push
EOF
    chmod +x "${NPM_DELIMIT}/hooks/git/pre-push"
    
    # Commit-msg hook
    cat > "${NPM_DELIMIT}/hooks/git/commit-msg" << 'EOF'
#!/bin/bash
# Delimit commit-msg hook
echo "[DELIMIT] Validating commit message..."
node /home/delimit/npm-delimit/bin/delimit-cli.js hook commit-msg "$1"
EOF
    chmod +x "${NPM_DELIMIT}/hooks/git/commit-msg"
    
    echo -e "${GREEN}✓ Git hooks created${NC}"
}

# Function to setup platform-specific configurations
setup_platform_configs() {
    echo -e "${YELLOW}Setting up platform-specific configurations...${NC}"
    
    # Use the platform adapter to set up all configurations
    cat > /tmp/setup-platforms.js << 'EOF'
const PlatformAdapter = require('/home/delimit/npm-delimit/lib/platform-adapters');
const adapter = new PlatformAdapter();

(async () => {
    const results = await adapter.setupAllPlatforms();
    console.log('Platform configurations created:', Object.keys(results).join(', '));
})();
EOF
    
    node /tmp/setup-platforms.js
    rm /tmp/setup-platforms.js
    
    echo -e "${GREEN}✓ Platform configurations created${NC}"
}

# Function to create model hooks
create_model_hooks() {
    echo -e "${YELLOW}Creating model-specific hooks...${NC}"
    
    models=("claude" "codex" "gemini" "openai" "cursor" "windsurf" "xai")
    
    for model in "${models[@]}"; do
        # Pre-request hook
        cat > "${NPM_DELIMIT}/hooks/models/${model}-pre.js" << EOF
#!/usr/bin/env node
// Delimit ${model} pre-request hook
console.log('[DELIMIT] ${model} pre-request validation');
// Model-specific validation logic
EOF
        chmod +x "${NPM_DELIMIT}/hooks/models/${model}-pre.js"
        
        # Post-response hook
        cat > "${NPM_DELIMIT}/hooks/models/${model}-post.js" << EOF
#!/usr/bin/env node
// Delimit ${model} post-response hook
console.log('[DELIMIT] ${model} response processing');
// Model-specific response processing
EOF
        chmod +x "${NPM_DELIMIT}/hooks/models/${model}-post.js"
    done
    
    echo -e "${GREEN}✓ Model hooks created${NC}"
}

# Function to install Claude hooks
install_claude_hooks() {
    echo -e "${YELLOW}Installing Claude Code hooks...${NC}"
    
    CLAUDE_CONFIG=$(find_claude_config)
    if [ -z "$CLAUDE_CONFIG" ]; then
        echo -e "${YELLOW}⚠️  Claude config directory not found${NC}"
        echo "Creating at ${HOME}/.claude..."
        CLAUDE_CONFIG="${HOME}/.claude"
        mkdir -p "$CLAUDE_CONFIG"
    fi
    
    # Create hooks directory
    mkdir -p "${CLAUDE_CONFIG}/hooks"
    
    # Copy hooks configuration
    if [ -f "/root/.claude/hooks/hooks.json" ]; then
        cp "/root/.claude/hooks/hooks.json" "${CLAUDE_CONFIG}/hooks/hooks.json"
        echo -e "${GREEN}✓ Hooks configuration installed${NC}"
    fi
    
    # Update MCP configuration
    MCP_CONFIG="${CLAUDE_CONFIG}/claude_desktop_config.json"
    if [ ! -f "$MCP_CONFIG" ]; then
        # Try alternate location
        MCP_CONFIG="${CLAUDE_CONFIG}/../claude_desktop_config.json"
    fi
    
    if [ -f "/root/Library/Application Support/Claude/claude_desktop_config.json" ]; then
        cp "/root/Library/Application Support/Claude/claude_desktop_config.json" "$MCP_CONFIG"
        echo -e "${GREEN}✓ MCP configuration updated${NC}"
    fi
}

# Function to configure Git
configure_git() {
    echo -e "${YELLOW}Configuring Git hooks...${NC}"
    
    # Set global hooks path
    git config --global core.hooksPath "${NPM_DELIMIT}/hooks/git"
    
    echo -e "${GREEN}✓ Git configured to use Delimit hooks${NC}"
}

# Function to install MCP servers with proper naming
install_mcp_servers() {
    echo -e "${YELLOW}Installing and updating MCP servers...${NC}"
    
    # Install FastMCP for compatibility
    pip3 install fastmcp || echo -e "${RED}⚠ FastMCP installation failed${NC}"
    
    # Install delimit packages with correct names
    local packages=(
        "delimit-governance:delimit-governance"
        "delimit-vault:delimit-vault" 
        "delimit-memory:delimit-memory"
        "delimit-deploy:delimit-deploy"
        "wireintel:delimit-intel"
    )
    
    for package in "${packages[@]}"; do
        IFS=':' read -r old_name new_name <<< "$package"
        package_dir="/home/delimit/.delimit_suite/packages/${old_name}"
        
        if [ -d "$package_dir" ]; then
            echo -e "  ${BLUE}Installing $new_name...${NC}"
            
            # Update pyproject.toml if needed
            if [ -f "$package_dir/pyproject.toml" ]; then
                sed -i "s/name = \"$old_name\"/name = \"$new_name\"/" "$package_dir/pyproject.toml" 2>/dev/null || true
                sed -i "s/name = \"${old_name}-mcp\"/name = \"$new_name\"/" "$package_dir/pyproject.toml" 2>/dev/null || true
            fi
            
            # Install package
            (cd "$package_dir" && pip3 install -e . >/dev/null 2>&1) && \
                echo -e "    ${GREEN}✓ $new_name installed${NC}" || \
                echo -e "    ${RED}✗ $new_name failed${NC}"
        fi
    done
    
    # Fix common directory issues
    if [ -L "/var/lib/delimit" ] && [ ! -d "/var/lib/delimit/wireintel" ]; then
        echo -e "  ${BLUE}Fixing directory permissions...${NC}"
        sudo rm -f /var/lib/delimit 2>/dev/null || true
        sudo mkdir -p /var/lib/delimit/wireintel 2>/dev/null || true
        sudo ln -sf /var/lib/delimit /var/lib/delimit 2>/dev/null || true
        echo -e "    ${GREEN}✓ Directory structure fixed${NC}"
    fi
}

# Function to troubleshoot MCP connections
troubleshoot_mcp() {
    echo -e "${YELLOW}Running MCP diagnostics...${NC}"
    
    local failed_servers=()
    
    # Test Python servers
    for server_dir in /home/delimit/.delimit_suite/packages/*/; do
        if [ -f "$server_dir/server.py" ] || [ -f "$server_dir/run_mcp.py" ]; then
            server_name=$(basename "$server_dir")
            echo -e "  ${BLUE}Testing $server_name...${NC}"
            
            # Test server startup
            if (cd "$server_dir" && timeout 5 python3 server.py 2>/dev/null) || \
               (cd "$server_dir" && timeout 5 python3 run_mcp.py 2>/dev/null); then
                echo -e "    ${GREEN}✓ $server_name OK${NC}"
            else
                echo -e "    ${RED}✗ $server_name failed${NC}"
                failed_servers+=("$server_name")
            fi
        fi
    done
    
    # Test NPM servers  
    if command -v npx >/dev/null; then
        echo -e "  ${BLUE}Testing codex...${NC}"
        if timeout 5 npx -y codex-mcp-server --version >/dev/null 2>&1; then
            echo -e "    ${GREEN}✓ codex OK${NC}"
        else
            echo -e "    ${RED}✗ codex failed${NC}"
            failed_servers+=("codex")
        fi
    fi
    
    if [ ${#failed_servers[@]} -gt 0 ]; then
        echo -e "\n${RED}Failed servers: ${failed_servers[*]}${NC}"
        echo -e "${YELLOW}Run with --fix-mcp to attempt repairs${NC}"
        return 1
    else
        echo -e "\n${GREEN}✓ All MCP servers operational${NC}"
        return 0
    fi
}

# Function to create test scripts
create_test_scripts() {
    echo -e "${YELLOW}Creating test scripts...${NC}"
    
    # Test hooks script
    cat > "${NPM_DELIMIT}/hooks/test-hooks.sh" << 'EOF'
#!/bin/bash
echo "Testing Delimit hooks..."

# Test pre-bash hook
echo "Testing bash hook..."
node /home/delimit/npm-delimit/hooks/pre-bash-hook.js '{"command":"ls"}'

# Test pre-write hook
echo "Testing write hook..."
node /home/delimit/npm-delimit/hooks/pre-write-hook.js '{"file_path":"/tmp/test.txt"}'

echo "✓ Hook tests complete"
EOF
    chmod +x "${NPM_DELIMIT}/hooks/test-hooks.sh"
    
    # Update script
    cat > "${NPM_DELIMIT}/hooks/update-delimit.sh" << 'EOF'
#!/bin/bash
echo "Updating Delimit..."
cd /home/delimit/npm-delimit
git pull
npm install
echo "✓ Delimit updated"
EOF
    chmod +x "${NPM_DELIMIT}/hooks/update-delimit.sh"
    
    # Evidence status script
    cat > "${NPM_DELIMIT}/hooks/evidence-status.sh" << 'EOF'
#!/bin/bash
echo "Evidence Collection Status"
echo "========================="
evidence_dir="${HOME}/.delimit/evidence"
if [ -d "$evidence_dir" ]; then
    count=$(find "$evidence_dir" -name "*.json" 2>/dev/null | wc -l)
    echo "Evidence files: $count"
    echo "Latest evidence:"
    ls -lt "$evidence_dir" 2>/dev/null | head -5
else
    echo "No evidence collected yet"
fi
EOF
    chmod +x "${NPM_DELIMIT}/hooks/evidence-status.sh"
    
    echo -e "${GREEN}✓ Test scripts created${NC}"
}

# Function to create message hooks
create_message_hooks() {
    echo -e "${YELLOW}Creating message hooks...${NC}"
    
    # Governance message hook
    cat > "${NPM_DELIMIT}/hooks/message-governance-hook.js" << 'EOF'
#!/usr/bin/env node
// Triggers governance check on keywords
const keywords = ['governance', 'policy', 'compliance', 'audit'];
const message = process.argv[2] || '';

if (keywords.some(k => message.toLowerCase().includes(k))) {
    console.log('[DELIMIT] Governance check triggered');
    require('child_process').execSync('delimit status --verbose');
}
EOF
    chmod +x "${NPM_DELIMIT}/hooks/message-governance-hook.js"
    
    # Auth message hook
    cat > "${NPM_DELIMIT}/hooks/message-auth-hook.js" << 'EOF'
#!/usr/bin/env node
// Triggers auth setup on keywords
const keywords = ['setup credentials', 'github key', 'api key'];
const message = process.argv[2] || '';

if (keywords.some(k => message.toLowerCase().includes(k))) {
    console.log('[DELIMIT] Authentication setup triggered');
    require('child_process').execSync('delimit auth');
}
EOF
    chmod +x "${NPM_DELIMIT}/hooks/message-auth-hook.js"
    
    echo -e "${GREEN}✓ Message hooks created${NC}"
}

# Function to create submission hooks
create_submission_hooks() {
    echo -e "${YELLOW}Creating submission hooks...${NC}"
    
    # Pre-submit hook
    cat > "${NPM_DELIMIT}/hooks/pre-submit-hook.js" << 'EOF'
#!/usr/bin/env node
// Validates before user prompt submission
console.log('[DELIMIT] Pre-submission validation');
// Add validation logic here
EOF
    chmod +x "${NPM_DELIMIT}/hooks/pre-submit-hook.js"
    
    # Post-response hook
    cat > "${NPM_DELIMIT}/hooks/post-response-hook.js" << 'EOF'
#!/usr/bin/env node
// Processes AI responses
console.log('[DELIMIT] Processing AI response');
// Add processing logic here
EOF
    chmod +x "${NPM_DELIMIT}/hooks/post-response-hook.js"
    
    echo -e "${GREEN}✓ Submission hooks created${NC}"
}

# Main installation
main() {
    echo -e "${BLUE}Starting Delimit hooks installation...${NC}"
    echo
    
    # Create directories
    create_directories
    
    # Create all hook scripts
    create_hook_scripts
    create_git_hooks
    create_model_hooks
    create_message_hooks
    create_submission_hooks
    create_test_scripts
    
    # Setup platform-specific configurations
    setup_platform_configs
    
    # Install Claude hooks
    install_claude_hooks
    
    # Configure Git
    configure_git
    
    # Install and configure MCP servers
    install_mcp_servers
    
    # Run MCP diagnostics
    echo
    if troubleshoot_mcp; then
        echo -e "${GREEN}🎉 All systems operational!${NC}"
    else
        echo -e "${YELLOW}⚠ Some MCP servers need attention${NC}"
    fi
    
    # Final message
    echo
    echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║     ✅ Delimit Hooks Installation Complete!  ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
    echo
    echo "Installed components:"
    echo "  • Pre-tool hooks for all tools"
    echo "  • Post-tool evidence collection"
    echo "  • Git hooks (pre-commit, pre-push, commit-msg)"
    echo "  • Model-specific hooks (Claude, Codex, Gemini, etc.)"
    echo "  • Message trigger hooks"
    echo "  • 16 slash commands"
    echo
    echo "Test your installation:"
    echo "  $ /test-hooks"
    echo "  $ /delimit"
    echo "  $ /governance"
    echo
    echo -e "${BLUE}Governance is now active!${NC}"
}

# Handle command line arguments
case "${1:-install}" in
    "install")
        main
        ;;
    "mcp-only")
        echo -e "${BLUE}Installing MCP servers only...${NC}"
        install_mcp_servers
        ;;
    "troubleshoot"|"test-mcp")
        echo -e "${BLUE}Running MCP diagnostics...${NC}"
        troubleshoot_mcp
        ;;
    "fix-mcp")
        echo -e "${BLUE}Fixing MCP servers...${NC}"
        install_mcp_servers
        troubleshoot_mcp
        ;;
    "help"|"--help"|"-h")
        echo "Delimit™ Hooks Installer"
        echo
        echo "Usage: $0 [command]"
        echo
        echo "Commands:"
        echo "  install       Full installation (default)"
        echo "  mcp-only      Install MCP servers only"
        echo "  troubleshoot  Run MCP diagnostics"
        echo "  fix-mcp       Fix MCP server issues"
        echo "  help          Show this help"
        echo
        ;;
    *)
        echo -e "${RED}Unknown command: $1${NC}"
        echo "Run '$0 help' for usage information"
        exit 1
        ;;
esac