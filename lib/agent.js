#!/usr/bin/env node

const express = require('express');
const fs = require('fs');
const path = require('path');
const yaml = require('js-yaml');
const minimatch = require('minimatch');
const { execSync } = require('child_process');
const DecisionEngine = require('./decision-engine');

class DelimitAgent {
    constructor() {
        this.app = express();
        this.app.use(express.json());
        
        // State
        this.sessionMode = 'auto';
        this.globalPolicies = {}; // Cached global/user/org policies
        this.projectPolicyCache = new Map(); // Cache for project-specific policies
        this.auditLog = [];
        this.port = process.env.DELIMIT_AGENT_PORT || 7823;
        this.decisionEngine = new DecisionEngine();
        this.startTime = Date.now();
        this.cacheExpiry = 5 * 60 * 1000; // 5 minute cache for project policies
        
        // Setup routes
        this.setupRoutes();
        
        // Load global policies only
        this.loadGlobalPolicies();
    }
    
    setupRoutes() {
        // Evaluate governance for an action
        this.app.post('/evaluate', (req, res) => {
            try {
                const context = req.body;
                const decision = this.evaluateGovernance(context);
                this.logDecision(decision, context);
                res.json(decision);
            } catch (error) {
                console.error('Error in /evaluate:', error);
                res.status(500).json({ error: 'Internal server error', action: 'allow' });
            }
        });
        
        // Set session mode
        this.app.post('/mode', (req, res) => {
            this.sessionMode = req.body.mode;
            res.json({ success: true, mode: this.sessionMode });
        });
        
        // Get status
        this.app.get('/status', (req, res) => {
            try {
                // Create dummy context for status check
                const dummyContext = { pwd: process.cwd() };
                const resolvedPolicies = this.resolvePoliciesForContext(dummyContext);
                const mergedPolicy = this.mergePoliciesWithSources(resolvedPolicies);
                
                const recentDecisions = this.auditLog.slice(-5).map(d => ({
                    timestamp: new Date(d.timestamp).toLocaleTimeString(),
                    mode: d.mode,
                    action: d.action,
                    rule: d.rule
                }));
                
                res.json({
                    sessionMode: this.sessionMode,
                    defaultMode: mergedPolicy.defaultMode || 'advisory',
                    effectiveMode: this.decisionEngine.lastDecision?.effectiveMode,
                    policiesLoaded: Object.keys(this.globalPolicies),
                    totalRules: mergedPolicy.rules?.length || 0,
                    auditLogSize: this.auditLog.length,
                    lastDecision: this.decisionEngine.lastDecision ? {
                        timestamp: this.decisionEngine.lastDecision.timestamp,
                        action: this.decisionEngine.lastDecision.action
                    } : null,
                    recentDecisions: recentDecisions,
                    uptime: process.uptime()
                });
            } catch (error) {
                console.error('Error in /status:', error);
                res.status(500).json({ error: 'Internal server error' });
            }
        });
        
        // Get recent audit log
        this.app.get('/audit', (req, res) => {
            res.json(this.auditLog.slice(-100));
        });
        
        // Explain a decision
        this.app.get('/explain/:id', (req, res) => {
            const id = req.params.id;
            let explanation;
            
            if (id === 'last') {
                explanation = this.decisionEngine.explainDecision();
            } else {
                explanation = this.decisionEngine.explainDecision(id);
            }
            
            if (explanation === 'No decision found') {
                res.status(404).json({ error: 'Decision not found' });
            } else {
                res.json({ explanation });
            }
        });
    }
    
    loadGlobalPolicies() {
        this.globalPolicies = {};
        
        // Load organization policy from environment or config
        const orgPolicyUrl = process.env.DELIMIT_ORG_POLICY_URL;
        if (orgPolicyUrl) {
            // TODO: Implement remote org policy fetching
            console.log('[Agent] Org policy URL configured:', orgPolicyUrl);
        }
        
        // Load user policy from standard location
        const userPolicyPath = path.join(process.env.HOME, '.config', 'delimit', 'delimit.yml');
        if (fs.existsSync(userPolicyPath)) {
            try {
                this.globalPolicies.user = {
                    policy: yaml.load(fs.readFileSync(userPolicyPath, 'utf8')),
                    path: userPolicyPath,
                    loadedAt: Date.now()
                };
                console.log('[Agent] Loaded user policy from', userPolicyPath);
            } catch (e) {
                console.error('[Agent] Failed to load user policy:', e.message);
            }
        }
        
        // Load system-wide default policy if exists
        const systemPolicyPath = '/etc/delimit/delimit.yml';
        if (fs.existsSync(systemPolicyPath)) {
            try {
                this.globalPolicies.system = {
                    policy: yaml.load(fs.readFileSync(systemPolicyPath, 'utf8')),
                    path: systemPolicyPath,
                    loadedAt: Date.now()
                };
                console.log('[Agent] Loaded system policy from', systemPolicyPath);
            } catch (e) {
                console.error('[Agent] Failed to load system policy:', e.message);
            }
        }
    }
    
    evaluateGovernance(context) {
        // Resolve policies for this specific context
        const resolvedPolicies = this.resolvePoliciesForContext(context);
        
        // Check for malformed project policy BEFORE merging
        if (resolvedPolicies.project?.error) {
            // Project policy is malformed - this is a critical error
            // Fail closed: block the action and report the error
            console.error('[Agent] CRITICAL: Project policy is malformed, failing closed');
            return {
                timestamp: new Date().toISOString(),
                mode: 'enforce',
                rule: 'MALFORMED_POLICY',
                action: 'block',
                message: `🛑 GOVERNANCE ERROR: Project policy at ${resolvedPolicies.project.path} is malformed\n` +
                        `Error: ${resolvedPolicies.project.error}\n` +
                        `Action blocked until policy is fixed.\n` +
                        `Fix the policy file or remove it to proceed.`,
                requiresOverride: true,
                error: true,
                policyError: resolvedPolicies.project.error
            };
        }
        
        // Merge policies in correct precedence order
        const mergedPolicy = this.mergePoliciesWithSources(resolvedPolicies);
        
        // The merged policy now contains accurate source information
        // No need to re-add it since mergePoliciesWithSources handles it correctly
        
        // Use the DecisionEngine for evaluation
        const decision = this.decisionEngine.makeDecision(context, mergedPolicy, this.sessionMode);
        
        // Convert to legacy format for backward compatibility
        return {
            timestamp: decision.timestamp,
            mode: decision.effectiveMode,
            rule: decision.matchedRules.length > 0 ? decision.matchedRules[0].name : null,
            action: decision.action,
            message: decision.message,
            requiresOverride: decision.action === 'block' && 
                              !mergedPolicy.overrides?.allowEnforceOverride,
            policySource: decision.explanation?.source || mergedPolicy.defaultModeSource,
            precedenceOrder: mergedPolicy._precedenceOrder
        };
    }
    
    ruleMatches(rule, context) {
        if (!rule.triggers) return false;
        
        for (const trigger of rule.triggers) {
            let matches = true;
            
            // Check path patterns
            if (trigger.path && context.files) {
                const pathMatches = context.files.some(file => 
                    minimatch(file, trigger.path)
                );
                matches = matches && pathMatches;
            }
            
            // Check content patterns
            if (trigger.content && context.diff) {
                const contentMatches = trigger.content.some(pattern =>
                    context.diff.includes(pattern)
                );
                matches = matches && contentMatches;
            }
            
            // Check command patterns
            if (trigger.command && context.command) {
                matches = matches && minimatch(context.command, trigger.command);
            }
            
            // Check git branch
            if (trigger.gitBranch && context.gitBranch) {
                matches = matches && trigger.gitBranch.includes(context.gitBranch);
            }
            
            // Check commit message
            if (trigger.commitMessage && context.commitMessage) {
                const regex = new RegExp(trigger.commitMessage);
                matches = matches && regex.test(context.commitMessage);
            }
            
            if (matches) return true;
        }
        
        return false;
    }
    
    getStrongerMode(mode1, mode2) {
        const strength = { advisory: 1, guarded: 2, enforce: 3 };
        return strength[mode2] > strength[mode1] ? mode2 : mode1;
    }
    
    makeDecision(mode, rule, context, policy) {
        const decision = {
            timestamp: new Date().toISOString(),
            mode: mode,
            rule: rule ? rule.name : null,
            action: 'allow',
            message: '',
            requiresOverride: false
        };
        
        switch (mode) {
            case 'advisory':
                decision.action = 'allow';
                decision.message = rule ? 
                    `[Advisory] ${rule.name} policy triggered` :
                    '[Advisory] Proceeding with standard checks';
                break;
                
            case 'guarded':
                decision.action = 'prompt';
                decision.message = rule ?
                    `[Guarded] ${rule.name} policy requires confirmation` :
                    '[Guarded] Action requires confirmation';
                decision.requiresOverride = true;
                break;
                
            case 'enforce':
                decision.action = 'block';
                decision.message = rule ?
                    `[Enforce] Blocked by ${rule.name} policy` :
                    '[Enforce] Action blocked by governance';
                decision.requiresOverride = !policy.overrides?.allowEnforceOverride;
                break;
        }
        
        return decision;
    }
    
    resolvePoliciesForContext(context) {
        const policies = {
            org: this.globalPolicies.org || null,
            system: this.globalPolicies.system || null,
            user: this.globalPolicies.user || null,
            project: null
        };
        
        // Resolve project policy for this specific context
        if (context.pwd) {
            policies.project = this.getProjectPolicy(context.pwd);
        }
        
        return policies;
    }
    
    getProjectPolicy(projectPath) {
        // Check cache first
        const cacheKey = projectPath;
        const cached = this.projectPolicyCache.get(cacheKey);
        
        if (cached && (Date.now() - cached.loadedAt) < this.cacheExpiry) {
            return cached;
        }
        
        // Load project policy from the actual project directory
        const policyPath = path.join(projectPath, 'delimit.yml');
        let projectPolicy = null;
        
        if (fs.existsSync(policyPath)) {
            try {
                const stat = fs.statSync(policyPath);
                const policyContent = fs.readFileSync(policyPath, 'utf8');
                const parsedPolicy = yaml.load(policyContent);
                
                // Validate the parsed policy
                if (!parsedPolicy || typeof parsedPolicy !== 'object') {
                    // Malformed policy - DO NOT fall back silently
                    projectPolicy = {
                        policy: null,
                        path: policyPath,
                        loadedAt: Date.now(),
                        modifiedAt: stat.mtime.getTime(),
                        error: 'Invalid YAML or empty policy file',
                        source: null
                    };
                    console.error(`[Agent] Project policy at ${policyPath} is malformed or empty`);
                } else {
                    projectPolicy = {
                        policy: parsedPolicy,
                        path: policyPath,
                        loadedAt: Date.now(),
                        modifiedAt: stat.mtime.getTime(),
                        source: 'project policy'
                    };
                }
                
                // Cache the result
                this.projectPolicyCache.set(cacheKey, projectPolicy);
                
            } catch (e) {
                console.error(`[Agent] Failed to load project policy from ${policyPath}:`, e.message);
                // Store the error state - DO NOT silently fall back
                projectPolicy = {
                    policy: null,
                    path: policyPath,
                    loadedAt: Date.now(),
                    error: e.message,
                    source: null
                };
                // Cache the failure to avoid repeated file system hits
                this.projectPolicyCache.set(cacheKey, projectPolicy);
            }
        } else {
            // No project policy file - this is OK, not an error
            projectPolicy = {
                policy: null,
                path: policyPath,
                loadedAt: Date.now(),
                source: null
            };
            // Cache the absence of project policy
            this.projectPolicyCache.set(cacheKey, projectPolicy);
        }
        
        return projectPolicy;
    }
    
    mergePoliciesWithSources(resolvedPolicies) {
        // Build merged policy with CORRECT precedence: project > user > system > org
        // Rules from higher-precedence policies completely override rules with the same name from lower-precedence policies
        const sources = [];
        const rules = [];
        const seenRuleNames = new Set();
        let defaultMode = 'advisory';
        let defaultModeSource = 'defaults';
        
        // Process policies in order of precedence (HIGHEST FIRST)
        const policiesInOrder = [
            { type: 'project', data: resolvedPolicies.project },
            { type: 'user', data: resolvedPolicies.user },
            { type: 'system', data: resolvedPolicies.system },
            { type: 'org', data: resolvedPolicies.org }
        ];
        
        // First pass: determine the effective defaultMode (highest precedence wins)
        for (const { type, data } of policiesInOrder) {
            if (data?.policy?.defaultMode) {
                if (defaultModeSource === 'defaults') {
                    defaultMode = data.policy.defaultMode;
                    defaultModeSource = `${type} policy`;
                    break; // Stop at first policy that defines defaultMode
                }
            }
        }
        
        // Second pass: merge rules with proper precedence
        for (const { type, data } of policiesInOrder) {
            if (!data) continue;
            
            // Handle malformed policies explicitly
            if (data.error) {
                console.error(`[Agent] ${type} policy is malformed: ${data.error}`);
                // Do NOT silently fall back - log the error and continue
                sources.push({ type, path: data.path, error: data.error });
                continue;
            }
            
            if (!data.policy) {
                // Policy file doesn't exist or is empty
                continue;
            }
            
            // Validate policy structure
            if (typeof data.policy !== 'object') {
                console.error(`[Agent] ${type} policy is not a valid object`);
                sources.push({ type, path: data.path, error: 'Invalid policy format' });
                continue;
            }
            
            sources.push({ type, path: data.path });
            
            // Process rules if they exist and are valid
            if (Array.isArray(data.policy.rules)) {
                for (const rule of data.policy.rules) {
                    // Rules must have names to be overridable
                    if (!rule.name) {
                        console.warn(`[Agent] Rule from ${type} policy missing 'name' property:`, rule);
                        continue;
                    }
                    
                    // Only add rule if we haven't seen this name from a higher-precedence source
                    if (!seenRuleNames.has(rule.name)) {
                        rules.push({ 
                            ...rule, 
                            source: `${type} policy`,
                            _sourcePath: data.path
                        });
                        seenRuleNames.add(rule.name);
                    } else {
                        // Log when a rule is overridden for debugging
                        console.debug(`[Agent] Rule '${rule.name}' from ${type} policy overridden by higher precedence`);
                    }
                }
            } else if (data.policy.rules !== undefined) {
                console.warn(`[Agent] ${type} policy has invalid 'rules' field (not an array)`);
            }
        }
        
        return {
            defaultMode,
            defaultModeSource,
            rules,
            _sources: sources,
            _resolvedAt: Date.now(),
            _context: resolvedPolicies,
            _precedenceOrder: 'project > user > system > org'
        };
    }
    
    logDecision(decision, context) {
        const logEntry = {
            ...decision,
            context: {
                command: context.command,
                pwd: context.pwd,
                user: process.env.USER,
                gitBranch: context.gitBranch
            }
        };
        
        this.auditLog.push(logEntry);
        
        // Also write to file
        const logDir = path.join(process.env.HOME, '.delimit', 'audit');
        fs.mkdirSync(logDir, { recursive: true });
        const logFile = path.join(logDir, `${new Date().toISOString().split('T')[0]}.jsonl`);
        fs.appendFileSync(logFile, JSON.stringify(logEntry) + '\n');
    }
    
    start() {
        this.app.listen(this.port, '127.0.0.1', () => {
            console.log(`[Delimit Agent] Running on port ${this.port}`);
            console.log(`[Delimit Agent] Session mode: ${this.sessionMode}`);
            console.log(`[Delimit Agent] Global policies loaded: ${Object.keys(this.globalPolicies).join(', ')}`);
        });
        
        // Reload global policies periodically and clear project cache
        setInterval(() => {
            this.loadGlobalPolicies();
            // Clear project policy cache to pick up changes
            this.projectPolicyCache.clear();
        }, 60000);
    }
}

// Start the agent
if (require.main === module) {
    const agent = new DelimitAgent();
    agent.start();
}

module.exports = DelimitAgent;