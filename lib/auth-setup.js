#!/usr/bin/env node

/**
 * Delimit Authentication Setup
 * Handles secure credential collection and storage for new users
 */

const readline = require('readline');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execSync } = require('child_process');
const chalk = require('chalk');

class DelimitAuthSetup {
    constructor() {
        this.configDir = path.join(process.env.HOME, '.delimit');
        this.credentialsFile = path.join(this.configDir, 'credentials.enc');
        this.authConfigFile = path.join(this.configDir, 'auth.json');
        
        // Ensure config directory exists
        if (!fs.existsSync(this.configDir)) {
            fs.mkdirSync(this.configDir, { recursive: true, mode: 0o700 });
        }
    }
    
    async setup(options = {}) {
        console.log(chalk.blue.bold('\n🔐 Delimit Authentication Setup\n'));
        
        const credentials = {};
        
        // Check for existing credentials
        if (fs.existsSync(this.credentialsFile) && !options.force) {
            const overwrite = await this.prompt(
                'Existing credentials found. Overwrite? (y/N): ',
                'n'
            );
            if (overwrite.toLowerCase() !== 'y') {
                console.log(chalk.yellow('Using existing credentials.'));
                return this.loadCredentials();
            }
        }
        
        // Collect credentials based on what's installed
        console.log(chalk.cyan('\n📋 Detecting installed tools...\n'));
        
        // GitHub credentials
        if (this.isInstalled('git')) {
            console.log(chalk.yellow('GitHub Configuration:'));
            credentials.github = await this.setupGitHub();
        }
        
        // AI Tool credentials
        const aiTools = {
            'claude': 'Anthropic Claude',
            'openai': 'OpenAI GPT',
            'gemini': 'Google Gemini',
            'codex': 'GitHub Copilot'
        };
        
        for (const [tool, name] of Object.entries(aiTools)) {
            if (this.isInstalled(tool) || options.all) {
                const setup = await this.prompt(
                    `\nSetup ${name}? (y/N): `,
                    'n'
                );
                if (setup.toLowerCase() === 'y') {
                    console.log(chalk.yellow(`\n${name} Configuration:`));
                    credentials[tool] = await this.setupAITool(tool, name);
                }
            }
        }
        
        // MCP Server credentials
        if (this.isInstalled('claude') || options.all) {
            const setupMcp = await this.prompt(
                '\nSetup MCP server authentication? (y/N): ',
                'n'
            );
            if (setupMcp.toLowerCase() === 'y') {
                console.log(chalk.yellow('\nMCP Server Configuration:'));
                credentials.mcp = await this.setupMCP();
            }
        }
        
        // Cloud Provider credentials
        const setupCloud = await this.prompt(
            '\nSetup cloud provider credentials? (y/N): ',
            'n'
        );
        if (setupCloud.toLowerCase() === 'y') {
            console.log(chalk.yellow('\nCloud Provider Configuration:'));
            credentials.cloud = await this.setupCloudProviders();
        }
        
        // Database credentials
        const setupDb = await this.prompt(
            '\nSetup database connections? (y/N): ',
            'n'
        );
        if (setupDb.toLowerCase() === 'y') {
            console.log(chalk.yellow('\nDatabase Configuration:'));
            credentials.databases = await this.setupDatabases();
        }
        
        // Container registries
        const setupRegistry = await this.prompt(
            '\nSetup container registries? (y/N): ',
            'n'
        );
        if (setupRegistry.toLowerCase() === 'y') {
            console.log(chalk.yellow('\nContainer Registry Configuration:'));
            credentials.registries = await this.setupRegistries();
        }
        
        // Package managers
        const setupPackages = await this.prompt(
            '\nSetup package manager credentials? (y/N): ',
            'n'
        );
        if (setupPackages.toLowerCase() === 'y') {
            console.log(chalk.yellow('\nPackage Manager Configuration:'));
            credentials.packages = await this.setupPackageManagers();
        }
        
        // Monitoring and observability
        const setupMonitoring = await this.prompt(
            '\nSetup monitoring services? (y/N): ',
            'n'
        );
        if (setupMonitoring.toLowerCase() === 'y') {
            console.log(chalk.yellow('\nMonitoring Configuration:'));
            credentials.monitoring = await this.setupMonitoring();
        }
        
        // Organization settings
        const setupOrg = await this.prompt(
            '\nSetup organization policies? (y/N): ',
            'n'
        );
        if (setupOrg.toLowerCase() === 'y') {
            console.log(chalk.yellow('\nOrganization Configuration:'));
            credentials.organization = await this.setupOrganization();
        }
        
        // Save credentials securely
        await this.saveCredentials(credentials);
        
        // Configure Git globally if GitHub was setup
        if (credentials.github) {
            await this.configureGit(credentials.github);
        }
        
        // Create environment file
        await this.createEnvironmentFile(credentials);
        
        console.log(chalk.green.bold('\n✅ Authentication setup complete!\n'));
        this.printSummary(credentials);
        
        return credentials;
    }
    
    async setupGitHub() {
        const github = {};
        
        // Check for existing Git config
        try {
            github.username = execSync('git config --global user.name', { encoding: 'utf8' }).trim();
            github.email = execSync('git config --global user.email', { encoding: 'utf8' }).trim();
            console.log(chalk.gray(`  Found: ${github.username} <${github.email}>`));
        } catch (e) {
            github.username = await this.prompt('  GitHub username: ');
            github.email = await this.prompt('  GitHub email: ');
        }
        
        // GitHub Personal Access Token
        const needToken = await this.prompt('  Add GitHub Personal Access Token? (y/N): ', 'n');
        if (needToken.toLowerCase() === 'y') {
            github.token = await this.promptSecret('  GitHub PAT: ');
            
            // Token scopes
            console.log(chalk.gray('\n  Required scopes for full functionality:'));
            console.log(chalk.gray('    • repo (Full control of private repositories)'));
            console.log(chalk.gray('    • workflow (Update GitHub Action workflows)'));
            console.log(chalk.gray('    • write:packages (Upload packages to GitHub Package Registry)'));
            console.log(chalk.gray('    • read:org (Read org and team membership)'));
        }
        
        // SSH Key setup
        const needSsh = await this.prompt('  Setup SSH key for GitHub? (y/N): ', 'n');
        if (needSsh.toLowerCase() === 'y') {
            github.sshKey = await this.setupSSHKey(github.email);
        }
        
        // GitHub CLI token (for gh command)
        if (this.isInstalled('gh')) {
            const needGhToken = await this.prompt('  Setup GitHub CLI (gh) authentication? (y/N): ', 'n');
            if (needGhToken.toLowerCase() === 'y') {
                github.ghToken = await this.promptSecret('  GitHub CLI token: ');
            }
        }
        
        return github;
    }
    
    async setupAITool(tool, name) {
        const config = {};
        
        switch (tool) {
            case 'claude':
                config.apiKey = await this.promptSecret(`  Anthropic API key: `);
                config.model = await this.prompt(`  Default model (claude-3-opus-20240229): `, 'claude-3-opus-20240229');
                break;
                
            case 'openai':
                config.apiKey = await this.promptSecret(`  OpenAI API key: `);
                config.organization = await this.prompt(`  Organization ID (optional): `, '');
                config.model = await this.prompt(`  Default model (gpt-4): `, 'gpt-4');
                break;
                
            case 'gemini':
                config.apiKey = await this.promptSecret(`  Google AI API key: `);
                config.projectId = await this.prompt(`  GCP Project ID (optional): `, '');
                break;
                
            case 'codex':
                config.token = await this.promptSecret(`  GitHub Copilot token: `);
                break;
        }
        
        // Rate limits and safety
        config.maxTokens = await this.prompt(`  Max tokens per request (4000): `, '4000');
        config.rateLimit = await this.prompt(`  Requests per minute (10): `, '10');
        
        return config;
    }
    
    async setupMCP() {
        const mcp = {};
        
        console.log(chalk.gray('\n  MCP servers can require authentication for:'));
        console.log(chalk.gray('    • Database connections'));
        console.log(chalk.gray('    • API endpoints'));
        console.log(chalk.gray('    • Cloud services'));
        
        const servers = [
            'delimit-gov',
            'delimit-mem',
            'delimit-os',
            'delimit-vault',
            'delimit-deploy'
        ];
        
        for (const server of servers) {
            const needAuth = await this.prompt(`\n  Setup ${server} authentication? (y/N): `, 'n');
            if (needAuth.toLowerCase() === 'y') {
                mcp[server] = {};
                
                // Check what type of auth is needed
                const authType = await this.prompt('    Auth type (token/credentials/oauth): ', 'token');
                
                switch (authType) {
                    case 'token':
                        mcp[server].token = await this.promptSecret('    API token: ');
                        break;
                    case 'credentials':
                        mcp[server].username = await this.prompt('    Username: ');
                        mcp[server].password = await this.promptSecret('    Password: ');
                        break;
                    case 'oauth':
                        mcp[server].clientId = await this.prompt('    Client ID: ');
                        mcp[server].clientSecret = await this.promptSecret('    Client secret: ');
                        break;
                }
                
                mcp[server].endpoint = await this.prompt('    Endpoint URL (http://localhost:8080): ', 'http://localhost:8080');
            }
        }
        
        return mcp;
    }
    
    async setupCloudProviders() {
        const cloud = {};
        
        // AWS
        const setupAws = await this.prompt('  Setup AWS credentials? (y/N): ', 'n');
        if (setupAws.toLowerCase() === 'y') {
            cloud.aws = {};
            cloud.aws.accessKeyId = await this.prompt('    AWS Access Key ID: ');
            cloud.aws.secretAccessKey = await this.promptSecret('    AWS Secret Access Key: ');
            cloud.aws.region = await this.prompt('    Default region (us-east-1): ', 'us-east-1');
            
            const needMfa = await this.prompt('    MFA device ARN (optional): ', '');
            if (needMfa) cloud.aws.mfaDevice = needMfa;
        }
        
        // Google Cloud
        const setupGcp = await this.prompt('  Setup Google Cloud credentials? (y/N): ', 'n');
        if (setupGcp.toLowerCase() === 'y') {
            cloud.gcp = {};
            cloud.gcp.projectId = await this.prompt('    GCP Project ID: ');
            
            const keyFile = await this.prompt('    Service account key file path: ');
            if (keyFile && fs.existsSync(keyFile)) {
                cloud.gcp.keyFile = keyFile;
            } else {
                cloud.gcp.clientEmail = await this.prompt('    Service account email: ');
                cloud.gcp.privateKey = await this.promptSecret('    Private key (paste entire key): ');
            }
        }
        
        // Azure
        const setupAzure = await this.prompt('  Setup Azure credentials? (y/N): ', 'n');
        if (setupAzure.toLowerCase() === 'y') {
            cloud.azure = {};
            cloud.azure.tenantId = await this.prompt('    Azure Tenant ID: ');
            cloud.azure.clientId = await this.prompt('    Client ID: ');
            cloud.azure.clientSecret = await this.promptSecret('    Client Secret: ');
            cloud.azure.subscriptionId = await this.prompt('    Subscription ID: ');
        }
        
        // DigitalOcean
        const setupDo = await this.prompt('  Setup DigitalOcean credentials? (y/N): ', 'n');
        if (setupDo.toLowerCase() === 'y') {
            cloud.digitalocean = {};
            cloud.digitalocean.token = await this.promptSecret('    DigitalOcean API Token: ');
        }
        
        // Cloudflare
        const setupCf = await this.prompt('  Setup Cloudflare credentials? (y/N): ', 'n');
        if (setupCf.toLowerCase() === 'y') {
            cloud.cloudflare = {};
            cloud.cloudflare.email = await this.prompt('    Cloudflare email: ');
            cloud.cloudflare.apiKey = await this.promptSecret('    Global API Key: ');
            cloud.cloudflare.zoneId = await this.prompt('    Zone ID (optional): ', '');
        }
        
        return cloud;
    }
    
    async setupDatabases() {
        const databases = {};
        
        // PostgreSQL
        const setupPg = await this.prompt('  Setup PostgreSQL? (y/N): ', 'n');
        if (setupPg.toLowerCase() === 'y') {
            databases.postgresql = {};
            databases.postgresql.host = await this.prompt('    Host (localhost): ', 'localhost');
            databases.postgresql.port = await this.prompt('    Port (5432): ', '5432');
            databases.postgresql.database = await this.prompt('    Database name: ');
            databases.postgresql.username = await this.prompt('    Username: ');
            databases.postgresql.password = await this.promptSecret('    Password: ');
            databases.postgresql.ssl = await this.prompt('    Use SSL? (Y/n): ', 'y');
        }
        
        // MySQL
        const setupMysql = await this.prompt('  Setup MySQL? (y/N): ', 'n');
        if (setupMysql.toLowerCase() === 'y') {
            databases.mysql = {};
            databases.mysql.host = await this.prompt('    Host (localhost): ', 'localhost');
            databases.mysql.port = await this.prompt('    Port (3306): ', '3306');
            databases.mysql.database = await this.prompt('    Database name: ');
            databases.mysql.username = await this.prompt('    Username: ');
            databases.mysql.password = await this.promptSecret('    Password: ');
        }
        
        // MongoDB
        const setupMongo = await this.prompt('  Setup MongoDB? (y/N): ', 'n');
        if (setupMongo.toLowerCase() === 'y') {
            databases.mongodb = {};
            const useUri = await this.prompt('    Use connection URI? (Y/n): ', 'y');
            if (useUri.toLowerCase() === 'y') {
                databases.mongodb.uri = await this.promptSecret('    MongoDB URI: ');
            } else {
                databases.mongodb.host = await this.prompt('    Host (localhost): ', 'localhost');
                databases.mongodb.port = await this.prompt('    Port (27017): ', '27017');
                databases.mongodb.database = await this.prompt('    Database name: ');
                databases.mongodb.username = await this.prompt('    Username: ');
                databases.mongodb.password = await this.promptSecret('    Password: ');
            }
        }
        
        // Redis
        const setupRedis = await this.prompt('  Setup Redis? (y/N): ', 'n');
        if (setupRedis.toLowerCase() === 'y') {
            databases.redis = {};
            databases.redis.host = await this.prompt('    Host (localhost): ', 'localhost');
            databases.redis.port = await this.prompt('    Port (6379): ', '6379');
            databases.redis.password = await this.promptSecret('    Password (optional): ');
            databases.redis.db = await this.prompt('    Database number (0): ', '0');
        }
        
        return databases;
    }
    
    async setupRegistries() {
        const registries = {};
        
        // Docker Hub
        const setupDocker = await this.prompt('  Setup Docker Hub? (y/N): ', 'n');
        if (setupDocker.toLowerCase() === 'y') {
            registries.dockerhub = {};
            registries.dockerhub.username = await this.prompt('    Docker Hub username: ');
            registries.dockerhub.password = await this.promptSecret('    Docker Hub password: ');
            registries.dockerhub.email = await this.prompt('    Email: ');
        }
        
        // GitHub Container Registry
        const setupGhcr = await this.prompt('  Setup GitHub Container Registry? (y/N): ', 'n');
        if (setupGhcr.toLowerCase() === 'y') {
            registries.ghcr = {};
            registries.ghcr.username = await this.prompt('    GitHub username: ');
            registries.ghcr.token = await this.promptSecret('    Personal Access Token: ');
        }
        
        // AWS ECR
        const setupEcr = await this.prompt('  Setup AWS ECR? (y/N): ', 'n');
        if (setupEcr.toLowerCase() === 'y') {
            registries.ecr = {};
            registries.ecr.region = await this.prompt('    AWS Region: ');
            registries.ecr.registryId = await this.prompt('    Registry ID: ');
        }
        
        // Google Artifact Registry
        const setupGar = await this.prompt('  Setup Google Artifact Registry? (y/N): ', 'n');
        if (setupGar.toLowerCase() === 'y') {
            registries.gar = {};
            registries.gar.location = await this.prompt('    Location: ');
            registries.gar.repository = await this.prompt('    Repository name: ');
        }
        
        // Private registry
        const setupPrivate = await this.prompt('  Setup private registry? (y/N): ', 'n');
        if (setupPrivate.toLowerCase() === 'y') {
            registries.private = {};
            registries.private.url = await this.prompt('    Registry URL: ');
            registries.private.username = await this.prompt('    Username: ');
            registries.private.password = await this.promptSecret('    Password: ');
        }
        
        return registries;
    }
    
    async setupPackageManagers() {
        const packages = {};
        
        // NPM
        const setupNpm = await this.prompt('  Setup NPM registry? (y/N): ', 'n');
        if (setupNpm.toLowerCase() === 'y') {
            packages.npm = {};
            packages.npm.registry = await this.prompt('    Registry URL (https://registry.npmjs.org/): ', 'https://registry.npmjs.org/');
            packages.npm.token = await this.promptSecret('    Auth token: ');
            
            const needScope = await this.prompt('    Scoped packages (@org)? (y/N): ', 'n');
            if (needScope.toLowerCase() === 'y') {
                packages.npm.scope = await this.prompt('      Scope name: ');
            }
        }
        
        // PyPI
        const setupPypi = await this.prompt('  Setup PyPI? (y/N): ', 'n');
        if (setupPypi.toLowerCase() === 'y') {
            packages.pypi = {};
            packages.pypi.username = await this.prompt('    PyPI username: ');
            packages.pypi.password = await this.promptSecret('    PyPI password: ');
            packages.pypi.repository = await this.prompt('    Repository (pypi): ', 'pypi');
        }
        
        // Maven
        const setupMaven = await this.prompt('  Setup Maven? (y/N): ', 'n');
        if (setupMaven.toLowerCase() === 'y') {
            packages.maven = {};
            packages.maven.repository = await this.prompt('    Repository URL: ');
            packages.maven.username = await this.prompt('    Username: ');
            packages.maven.password = await this.promptSecret('    Password: ');
        }
        
        // RubyGems
        const setupGem = await this.prompt('  Setup RubyGems? (y/N): ', 'n');
        if (setupGem.toLowerCase() === 'y') {
            packages.rubygems = {};
            packages.rubygems.apiKey = await this.promptSecret('    API Key: ');
        }
        
        // Cargo (Rust)
        const setupCargo = await this.prompt('  Setup Cargo? (y/N): ', 'n');
        if (setupCargo.toLowerCase() === 'y') {
            packages.cargo = {};
            packages.cargo.token = await this.promptSecret('    Crates.io token: ');
        }
        
        return packages;
    }
    
    async setupMonitoring() {
        const monitoring = {};
        
        // Datadog
        const setupDatadog = await this.prompt('  Setup Datadog? (y/N): ', 'n');
        if (setupDatadog.toLowerCase() === 'y') {
            monitoring.datadog = {};
            monitoring.datadog.apiKey = await this.promptSecret('    API Key: ');
            monitoring.datadog.appKey = await this.promptSecret('    Application Key: ');
            monitoring.datadog.site = await this.prompt('    Site (datadoghq.com): ', 'datadoghq.com');
        }
        
        // New Relic
        const setupNewrelic = await this.prompt('  Setup New Relic? (y/N): ', 'n');
        if (setupNewrelic.toLowerCase() === 'y') {
            monitoring.newrelic = {};
            monitoring.newrelic.accountId = await this.prompt('    Account ID: ');
            monitoring.newrelic.apiKey = await this.promptSecret('    API Key: ');
            monitoring.newrelic.licenseKey = await this.promptSecret('    License Key: ');
        }
        
        // Sentry
        const setupSentry = await this.prompt('  Setup Sentry? (y/N): ', 'n');
        if (setupSentry.toLowerCase() === 'y') {
            monitoring.sentry = {};
            monitoring.sentry.dsn = await this.promptSecret('    DSN: ');
            monitoring.sentry.org = await this.prompt('    Organization slug: ');
            monitoring.sentry.project = await this.prompt('    Project slug: ');
            monitoring.sentry.authToken = await this.promptSecret('    Auth token: ');
        }
        
        // PagerDuty
        const setupPager = await this.prompt('  Setup PagerDuty? (y/N): ', 'n');
        if (setupPager.toLowerCase() === 'y') {
            monitoring.pagerduty = {};
            monitoring.pagerduty.apiKey = await this.promptSecret('    API Key: ');
            monitoring.pagerduty.integrationKey = await this.promptSecret('    Integration Key: ');
        }
        
        // Prometheus/Grafana
        const setupProm = await this.prompt('  Setup Prometheus/Grafana? (y/N): ', 'n');
        if (setupProm.toLowerCase() === 'y') {
            monitoring.prometheus = {};
            monitoring.prometheus.url = await this.prompt('    Prometheus URL: ');
            monitoring.grafana = {};
            monitoring.grafana.url = await this.prompt('    Grafana URL: ');
            monitoring.grafana.apiKey = await this.promptSecret('    Grafana API Key: ');
        }
        
        return monitoring;
    }
    
    async setupOrganization() {
        const org = {};
        
        org.name = await this.prompt('  Organization name: ');
        org.domain = await this.prompt('  Organization domain: ');
        
        // SSO/SAML
        const hasSso = await this.prompt('  Use SSO/SAML? (y/N): ', 'n');
        if (hasSso.toLowerCase() === 'y') {
            org.sso = {};
            org.sso.provider = await this.prompt('    Provider (okta/auth0/azure): ');
            org.sso.domain = await this.prompt('    SSO domain: ');
            org.sso.clientId = await this.prompt('    Client ID: ');
            org.sso.clientSecret = await this.promptSecret('    Client secret: ');
        }
        
        // Policy server
        const hasPolicyServer = await this.prompt('  Use organization policy server? (y/N): ', 'n');
        if (hasPolicyServer.toLowerCase() === 'y') {
            org.policyUrl = await this.prompt('    Policy server URL: ');
            org.policyToken = await this.promptSecret('    Policy server token: ');
        }
        
        // VPN/Proxy
        const hasVpn = await this.prompt('  Configure VPN/Proxy? (y/N): ', 'n');
        if (hasVpn.toLowerCase() === 'y') {
            org.proxy = {};
            org.proxy.http = await this.prompt('    HTTP proxy: ');
            org.proxy.https = await this.prompt('    HTTPS proxy: ');
            org.proxy.noProxy = await this.prompt('    No proxy (localhost,127.0.0.1): ', 'localhost,127.0.0.1');
        }
        
        // Audit requirements
        org.auditLevel = await this.prompt('  Audit level (basic/detailed/verbose): ', 'detailed');
        org.requireApproval = await this.prompt('  Require approval for enforce mode? (Y/n): ', 'y');
        
        return org;
    }
    
    async setupSSHKey(email) {
        const sshDir = path.join(process.env.HOME, '.ssh');
        const keyPath = path.join(sshDir, 'id_ed25519_delimit');
        
        // Check for existing key
        if (fs.existsSync(keyPath)) {
            console.log(chalk.gray(`  Found existing SSH key: ${keyPath}`));
            return keyPath;
        }
        
        // Generate new key
        console.log(chalk.cyan('  Generating new SSH key...'));
        
        if (!fs.existsSync(sshDir)) {
            fs.mkdirSync(sshDir, { mode: 0o700 });
        }
        
        try {
            execSync(`ssh-keygen -t ed25519 -C "${email}" -f ${keyPath} -N ""`, { stdio: 'pipe' });
            
            // Set proper permissions
            fs.chmodSync(keyPath, 0o600);
            fs.chmodSync(`${keyPath}.pub`, 0o644);
            
            // Display public key
            const publicKey = fs.readFileSync(`${keyPath}.pub`, 'utf8');
            console.log(chalk.green('\n  SSH key generated successfully!'));
            console.log(chalk.cyan('\n  Add this public key to GitHub:'));
            console.log(chalk.white(`  ${publicKey}`));
            console.log(chalk.gray('\n  https://github.com/settings/keys'));
            
            // Add to SSH agent
            try {
                execSync(`ssh-add ${keyPath}`, { stdio: 'pipe' });
                console.log(chalk.green('  Added to SSH agent'));
            } catch (e) {
                console.log(chalk.yellow('  Could not add to SSH agent (start ssh-agent first)'));
            }
            
            return keyPath;
        } catch (e) {
            console.log(chalk.red('  Failed to generate SSH key:', e.message));
            return null;
        }
    }
    
    async configureGit(github) {
        console.log(chalk.cyan('\n🔧 Configuring Git...'));
        
        try {
            // Set user info
            if (github.username) {
                execSync(`git config --global user.name "${github.username}"`, { stdio: 'pipe' });
            }
            if (github.email) {
                execSync(`git config --global user.email "${github.email}"`, { stdio: 'pipe' });
            }
            
            // Set up credential helper
            if (github.token) {
                // Create credentials file for HTTPS
                const credFile = path.join(this.configDir, 'git-credentials');
                const credContent = `https://${github.username}:${github.token}@github.com\n`;
                fs.writeFileSync(credFile, credContent, { mode: 0o600 });
                
                execSync(`git config --global credential.helper "store --file=${credFile}"`, { stdio: 'pipe' });
                console.log(chalk.green('  ✓ Git credentials configured'));
            }
            
            // Configure Delimit hooks
            const hooksPath = path.join(this.configDir, 'hooks');
            execSync(`git config --global core.hooksPath ${hooksPath}`, { stdio: 'pipe' });
            console.log(chalk.green('  ✓ Delimit hooks configured'));
            
        } catch (e) {
            console.log(chalk.yellow('  Warning: Could not configure Git:', e.message));
        }
    }
    
    async createEnvironmentFile(credentials) {
        const envFile = path.join(this.configDir, 'env.sh');
        let envContent = '#!/bin/bash\n# Delimit Environment Configuration\n\n';
        
        // GitHub
        if (credentials.github?.token) {
            envContent += `export GITHUB_TOKEN="${credentials.github.token}"\n`;
        }
        if (credentials.github?.ghToken) {
            envContent += `export GH_TOKEN="${credentials.github.ghToken}"\n`;
        }
        
        // AI Tools
        if (credentials.claude?.apiKey) {
            envContent += `export ANTHROPIC_API_KEY="${credentials.claude.apiKey}"\n`;
        }
        if (credentials.openai?.apiKey) {
            envContent += `export OPENAI_API_KEY="${credentials.openai.apiKey}"\n`;
        }
        if (credentials.gemini?.apiKey) {
            envContent += `export GOOGLE_AI_API_KEY="${credentials.gemini.apiKey}"\n`;
        }
        
        // Organization
        if (credentials.organization?.policyUrl) {
            envContent += `export DELIMIT_ORG_POLICY_URL="${credentials.organization.policyUrl}"\n`;
            envContent += `export DELIMIT_ORG_POLICY_TOKEN="${credentials.organization.policyToken}"\n`;
        }
        
        // Delimit settings
        envContent += `\n# Delimit Settings\n`;
        envContent += `export DELIMIT_CONFIGURED=true\n`;
        envContent += `export DELIMIT_AUTH_FILE="${this.credentialsFile}"\n`;
        
        fs.writeFileSync(envFile, envContent, { mode: 0o600 });
        
        // Add to bashrc if not present
        const bashrcPath = path.join(process.env.HOME, '.bashrc');
        const sourceLine = `source ${envFile}`;
        
        if (fs.existsSync(bashrcPath)) {
            const bashrc = fs.readFileSync(bashrcPath, 'utf8');
            if (!bashrc.includes(sourceLine)) {
                fs.appendFileSync(bashrcPath, `\n# Delimit Authentication\n${sourceLine}\n`);
            }
        }
    }
    
    async saveCredentials(credentials) {
        // Generate encryption key from machine ID
        const machineId = this.getMachineId();
        const key = crypto.createHash('sha256').update(machineId).digest();
        
        // Encrypt credentials
        const iv = crypto.randomBytes(16);
        const cipher = crypto.createCipheriv('aes-256-cbc', key, iv);
        
        const encrypted = Buffer.concat([
            iv,
            cipher.update(JSON.stringify(credentials, null, 2)),
            cipher.final()
        ]);
        
        // Save encrypted file
        fs.writeFileSync(this.credentialsFile, encrypted, { mode: 0o600 });
        
        // Save auth config (non-sensitive)
        const authConfig = {
            configured: true,
            timestamp: new Date().toISOString(),
            tools: Object.keys(credentials)
        };
        fs.writeFileSync(this.authConfigFile, JSON.stringify(authConfig, null, 2));
    }
    
    async loadCredentials() {
        if (!fs.existsSync(this.credentialsFile)) {
            return null;
        }
        
        try {
            // Generate decryption key
            const machineId = this.getMachineId();
            const key = crypto.createHash('sha256').update(machineId).digest();
            
            // Read encrypted file
            const encrypted = fs.readFileSync(this.credentialsFile);
            const iv = encrypted.slice(0, 16);
            const data = encrypted.slice(16);
            
            // Decrypt
            const decipher = crypto.createDecipheriv('aes-256-cbc', key, iv);
            const decrypted = Buffer.concat([
                decipher.update(data),
                decipher.final()
            ]);
            
            return JSON.parse(decrypted.toString());
        } catch (e) {
            console.log(chalk.red('Failed to load credentials:', e.message));
            return null;
        }
    }
    
    getMachineId() {
        // Get a unique machine identifier
        try {
            const hostname = require('os').hostname();
            const cpuInfo = require('os').cpus()[0].model;
            return `${hostname}-${cpuInfo}`;
        } catch (e) {
            return 'default-machine-id';
        }
    }
    
    isInstalled(tool) {
        try {
            execSync(`which ${tool}`, { stdio: 'ignore' });
            return true;
        } catch {
            return false;
        }
    }
    
    prompt(question, defaultValue = '') {
        const rl = readline.createInterface({
            input: process.stdin,
            output: process.stdout
        });
        
        return new Promise((resolve) => {
            rl.question(question, (answer) => {
                rl.close();
                resolve(answer || defaultValue);
            });
        });
    }
    
    promptSecret(question) {
        const rl = readline.createInterface({
            input: process.stdin,
            output: process.stdout
        });
        
        return new Promise((resolve) => {
            // Hide input
            process.stdout.write(question);
            
            let secret = '';
            process.stdin.setRawMode(true);
            process.stdin.resume();
            process.stdin.setEncoding('utf8');
            
            const onData = (char) => {
                char = char.toString('utf8');
                
                switch (char) {
                    case '\n':
                    case '\r':
                    case '\u0004':
                        process.stdin.setRawMode(false);
                        process.stdin.pause();
                        process.stdin.removeListener('data', onData);
                        process.stdout.write('\n');
                        rl.close();
                        resolve(secret);
                        break;
                    case '\u0003':
                        process.exit();
                        break;
                    case '\u007f':
                        if (secret.length > 0) {
                            secret = secret.slice(0, -1);
                            process.stdout.write('\b \b');
                        }
                        break;
                    default:
                        secret += char;
                        process.stdout.write('*');
                        break;
                }
            };
            
            process.stdin.on('data', onData);
        });
    }
    
    printSummary(credentials) {
        console.log(chalk.cyan('📋 Configuration Summary:'));
        
        if (credentials.github) {
            console.log(chalk.white(`  • GitHub: ${credentials.github.username} <${credentials.github.email}>`));
            if (credentials.github.token) console.log(chalk.gray('    - Personal Access Token configured'));
            if (credentials.github.sshKey) console.log(chalk.gray('    - SSH key configured'));
            if (credentials.github.ghToken) console.log(chalk.gray('    - GitHub CLI configured'));
        }
        
        for (const tool of ['claude', 'openai', 'gemini', 'codex']) {
            if (credentials[tool]) {
                console.log(chalk.white(`  • ${tool}: Configured`));
            }
        }
        
        if (credentials.mcp && Object.keys(credentials.mcp).length > 0) {
            console.log(chalk.white(`  • MCP Servers: ${Object.keys(credentials.mcp).length} configured`));
        }
        
        if (credentials.organization) {
            console.log(chalk.white(`  • Organization: ${credentials.organization.name}`));
        }
        
        console.log(chalk.cyan('\n🎯 Next Steps:'));
        console.log(chalk.white('  1. Restart your shell to load environment'));
        console.log(chalk.white('  2. Run "delimit test auth" to verify credentials'));
        console.log(chalk.white('  3. Run "delimit install --hooks all" to complete setup'));
    }
}

// Export for use as module
module.exports = DelimitAuthSetup;

// Run if executed directly
if (require.main === module) {
    const setup = new DelimitAuthSetup();
    setup.setup({ all: process.argv.includes('--all') }).catch(console.error);
}