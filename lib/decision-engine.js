class DecisionEngine {
    constructor() {
        this.lastDecision = null;
        this.decisionHistory = [];
    }
    
    makeDecision(context, policies, sessionMode) {
        const decision = {
            timestamp: new Date().toISOString(),
            id: this.generateId(),
            
            // Modes
            configuredMode: sessionMode,
            defaultMode: policies.defaultMode || 'advisory',
            effectiveMode: null,
            
            // Reasoning
            matchedRules: [],
            escalationPath: [],
            policySource: this.getPolicySources(policies),
            
            // Decision
            action: null,
            message: null,
            
            // Explanation
            explanation: {
                why: null,
                trigger: null,
                rule: null,
                source: null
            },
            
            // Context
            context: {
                command: context.command,
                pwd: context.pwd,
                gitBranch: context.gitBranch,
                files: context.files || [],
                user: process.env.USER
            }
        };
        
        // Start with base mode
        let currentMode = sessionMode === 'auto' ? 
            (policies.defaultMode || 'advisory') : 
            sessionMode;
            
        decision.escalationPath.push({
            mode: currentMode,
            reason: sessionMode === 'auto' ? 
                `Default mode from ${this.findPolicySource(policies, 'defaultMode')}` :
                'User-configured session mode'
        });
        
        // Evaluate all rules
        if (policies.rules) {
            for (const rule of policies.rules) {
                const match = this.evaluateRule(rule, context);
                if (match) {
                    decision.matchedRules.push({
                        name: rule.name,
                        mode: rule.mode,
                        trigger: match.trigger,
                        triggerType: match.type,
                        source: match.source || 'local'
                    });
                    
                    // Check if this rule escalates
                    if (this.isStrongerMode(rule.mode, currentMode)) {
                        currentMode = rule.mode;
                        decision.escalationPath.push({
                            mode: rule.mode,
                            reason: `Rule "${rule.name}" matched`,
                            trigger: match.trigger,
                            source: match.source
                        });
                    }
                    
                    if (rule.final) {
                        decision.explanation.why = `Final rule "${rule.name}" matched`;
                        break;
                    }
                }
            }
        }
        
        decision.effectiveMode = currentMode;
        
        // Build explanation
        if (decision.matchedRules.length > 0) {
            const strongestRule = decision.matchedRules.reduce((a, b) => 
                this.isStrongerMode(b.mode, a.mode) ? b : a
            );
            
            decision.explanation = {
                why: `Mode escalated to ${currentMode.toUpperCase()}`,
                trigger: strongestRule.trigger,
                rule: strongestRule.name,
                source: strongestRule.source || this.findPolicySource(policies, 'rules')
            };
        } else {
            decision.explanation = {
                why: `Using ${currentMode} mode`,
                trigger: 'No matching rules',
                rule: null,
                source: sessionMode === 'auto' ? 
                    this.findPolicySource(policies, 'defaultMode') :
                    'user configuration'
            };
        }
        
        // Determine action based on effective mode
        switch (currentMode) {
            case 'advisory':
                decision.action = 'allow';
                decision.message = this.formatMessage('advisory', decision);
                break;
                
            case 'guarded':
                decision.action = 'prompt';
                decision.message = this.formatMessage('guarded', decision);
                break;
                
            case 'enforce':
                decision.action = 'block';
                decision.message = this.formatMessage('enforce', decision);
                break;
        }
        
        // Store for later retrieval
        this.lastDecision = decision;
        this.decisionHistory.push(decision);
        if (this.decisionHistory.length > 100) {
            this.decisionHistory.shift();
        }
        
        return decision;
    }
    
    evaluateRule(rule, context) {
        if (!rule.triggers) return null;
        
        for (const trigger of rule.triggers) {
            // Check path patterns
            if (trigger.path && context.files) {
                for (const file of context.files) {
                    if (this.matchesPattern(file, trigger.path)) {
                        return {
                            type: 'path',
                            trigger: `path matches "${trigger.path}"`,
                            matched: file,
                            source: rule.source
                        };
                    }
                }
            }
            
            // Check content patterns
            if (trigger.content && context.diff) {
                for (const pattern of trigger.content) {
                    if (context.diff.includes(pattern)) {
                        return {
                            type: 'content',
                            trigger: `content contains "${pattern}"`,
                            matched: pattern,
                            source: rule.source
                        };
                    }
                }
            }
            
            // Check git branch
            if (trigger.gitBranch && context.gitBranch) {
                if (trigger.gitBranch.includes(context.gitBranch)) {
                    return {
                        type: 'branch',
                        trigger: `branch is "${context.gitBranch}"`,
                        matched: context.gitBranch,
                        source: rule.source
                    };
                }
            }
            
            // Check command patterns
            if (trigger.command && context.command) {
                if (this.matchesPattern(context.command, trigger.command)) {
                    return {
                        type: 'command',
                        trigger: `command matches "${trigger.command}"`,
                        matched: context.command,
                        source: rule.source
                    };
                }
            }
        }
        
        return null;
    }
    
    formatMessage(mode, decision) {
        const colors = {
            advisory: '\x1b[34m',  // blue
            guarded: '\x1b[33m',   // yellow
            enforce: '\x1b[31m'    // red
        };
        const reset = '\x1b[0m';
        const bold = '\x1b[1m';
        
        let msg = `${colors[mode]}${bold}[Delimit ${mode.toUpperCase()}]${reset}\n`;
        
        if (decision.matchedRules.length > 0) {
            const rule = decision.matchedRules[0];
            msg += `📋 Rule: "${rule.name}"\n`;
            msg += `🎯 Trigger: ${rule.trigger}\n`;
            msg += `📁 Policy: ${rule.source || 'local'}\n`;
        }
        
        if (decision.configuredMode !== decision.effectiveMode) {
            msg += `⚡ Mode escalated from ${decision.configuredMode} → ${decision.effectiveMode}\n`;
        }
        
        // Add specific reason and action guidance
        switch (mode) {
            case 'advisory':
                msg += `✅ Proceeding with standard checks\n`;
                msg += `💡 Recommendation: Review changes before committing`;
                break;
            case 'guarded':
                msg += `⚠️  Confirmation required to proceed\n`;
                msg += `💡 To proceed: Confirm when prompted, or use --force flag\n`;
                msg += `💡 To avoid: Switch to a feature branch or request override`;
                break;
            case 'enforce':
                msg += `🛑 BLOCKED: ${this.getBlockReason(decision)}\n`;
                msg += `\n💡 HOW TO PROCEED:\n`;
                if (decision.context.gitBranch && ['main', 'master', 'production'].includes(decision.context.gitBranch)) {
                    msg += `  1. Switch to a feature branch: git checkout -b feature/your-change\n`;
                    msg += `  2. Commit your changes there\n`;
                    msg += `  3. Create a pull request for review\n`;
                }
                if (decision.matchedRules.some(r => r.trigger.includes('payment'))) {
                    msg += `  1. Payment code requires security review\n`;
                    msg += `  2. Request review from security team\n`;
                    msg += `  3. Use approved payment SDK methods only\n`;
                }
                msg += `\n📖 Policy location: ${this.getPolicyLocation(decision)}`;
                break;
        }
        
        return msg;
    }
    
    getBlockReason(decision) {
        if (decision.matchedRules.length === 0) {
            return 'Enforce mode is active (no specific rule matched)';
        }
        const rule = decision.matchedRules[0];
        if (rule.name === 'Production Protection') {
            return `Direct commits to ${decision.context.gitBranch} branch are prohibited`;
        }
        if (rule.name === 'Payment Code Security') {
            return 'Payment/billing code changes require security review';
        }
        return `Rule "${rule.name}" prohibits this action`;
    }
    
    getPolicyLocation(decision) {
        const sources = [];
        if (decision.matchedRules.length > 0) {
            const rule = decision.matchedRules[0];
            if (rule.source === 'project policy') {
                sources.push('./delimit.yml');
            } else if (rule.source === 'user policy') {
                sources.push('~/.config/delimit/delimit.yml');
            } else if (rule.source === 'org policy') {
                sources.push('Organization policy (contact admin)');
            }
        }
        return sources.length > 0 ? sources.join(', ') : 'delimit.yml (default location)';
    }
    
    explainDecision(decisionId) {
        const decision = decisionId ? 
            this.decisionHistory.find(d => d.id === decisionId) :
            this.lastDecision;
            
        if (!decision) {
            return 'No decision found';
        }
        
        let explanation = '\n📊 GOVERNANCE DECISION EXPLANATION\n';
        explanation += '═══════════════════════════════════\n\n';
        
        explanation += `Decision ID: ${decision.id}\n`;
        explanation += `Timestamp: ${decision.timestamp}\n\n`;
        
        // PRIMARY REASON - Clear and upfront
        explanation += '❌ WHY BLOCKED\n';
        if (decision.action === 'block') {
            explanation += this.getDetailedBlockReason(decision) + '\n\n';
        } else if (decision.action === 'prompt') {
            explanation += `Confirmation required: ${decision.explanation.why}\n\n`;
        } else {
            explanation += `Allowed: ${decision.explanation.why}\n\n`;
        }
        
        // HOW TO PROCEED
        if (decision.action !== 'allow') {
            explanation += '✅ HOW TO PROCEED\n';
            explanation += this.getActionGuidance(decision) + '\n';
        }
        
        // TRIGGERING CONTEXT
        explanation += '📍 WHAT TRIGGERED THIS\n';
        if (decision.context.files && decision.context.files.length > 0) {
            explanation += `├─ Files modified:\n`;
            decision.context.files.slice(0, 5).forEach(f => {
                explanation += `│  • ${f}\n`;
            });
            if (decision.context.files.length > 5) {
                explanation += `│  • ... and ${decision.context.files.length - 5} more\n`;
            }
        }
        explanation += `├─ Branch: ${decision.context.gitBranch || 'unknown'}\n`;
        explanation += `├─ Directory: ${decision.context.pwd}\n`;
        explanation += `└─ User: ${decision.context.user}\n\n`;
        
        // POLICY SOURCE
        explanation += '📖 POLICY DETAILS\n';
        if (decision.matchedRules.length > 0) {
            const rule = decision.matchedRules[0];
            explanation += `├─ Rule: "${rule.name}"\n`;
            explanation += `├─ Location: ${this.getPolicyFileLocation(rule.source)}\n`;
            explanation += `├─ Trigger type: ${rule.triggerType}\n`;
            explanation += `└─ Exact trigger: ${rule.trigger}\n`;
        } else {
            explanation += `├─ No specific rule matched\n`;
            explanation += `└─ Using default mode: ${decision.defaultMode}\n`;
        }
        explanation += '\n';
        
        // MODE DETAILS (less prominent)
        explanation += '🎚️  MODE DETAILS\n';
        explanation += `├─ Session mode: ${decision.configuredMode}\n`;
        explanation += `├─ Default mode: ${decision.defaultMode}\n`;
        explanation += `└─ Effective mode: ${decision.effectiveMode}\n`;
        
        if (decision.escalationPath.length > 1) {
            explanation += '\n📈 ESCALATION HISTORY\n';
            decision.escalationPath.forEach((step, i) => {
                const prefix = i === decision.escalationPath.length - 1 ? '└─' : '├─';
                explanation += `${prefix} ${step.mode}: ${step.reason}\n`;
            });
        }
        
        // AUDIT TRAIL
        explanation += '\n🔍 AUDIT\n';
        explanation += `├─ Decision ID: ${decision.id}\n`;
        explanation += `├─ Timestamp: ${decision.timestamp}\n`;
        explanation += `└─ Audit log: ~/.delimit/audit/${new Date(decision.timestamp).toISOString().split('T')[0]}.jsonl\n`;
        
        return explanation;
    }
    
    getDetailedBlockReason(decision) {
        if (decision.matchedRules.length === 0) {
            return 'Enforce mode is active but no specific rule matched.\nThis suggests a configuration issue.';
        }
        
        const rule = decision.matchedRules[0];
        let reason = '';
        
        if (rule.name === 'Production Protection') {
            reason = `Direct commits to the ${decision.context.gitBranch} branch are prohibited.\n`;
            reason += `This branch is protected to prevent accidental production changes.`;
        } else if (rule.name === 'Payment Code Security') {
            reason = `Changes to payment/billing code require security review.\n`;
            reason += `Files matching "${rule.trigger}" triggered this protection.`;
        } else if (rule.name === 'AI-Generated Code Review') {
            reason = `AI-generated code requires human review before committing.\n`;
            reason += `Detected by: ${rule.trigger}`;
        } else {
            reason = `Rule "${rule.name}" prohibits this action.\n`;
            reason += `Trigger: ${rule.trigger}`;
        }
        
        return reason;
    }
    
    getActionGuidance(decision) {
        let guidance = '';
        
        if (decision.action === 'block') {
            if (decision.context.gitBranch && ['main', 'master', 'production'].includes(decision.context.gitBranch)) {
                guidance += `1. Switch to a feature branch:\n`;
                guidance += `   git checkout -b feature/your-change\n`;
                guidance += `2. Commit your changes there\n`;
                guidance += `3. Create a pull request for review\n`;
            } else if (decision.matchedRules.some(r => r.trigger && r.trigger.includes('payment'))) {
                guidance += `1. Request security review for payment code\n`;
                guidance += `2. Use approved payment SDK methods\n`;
                guidance += `3. Add security tests for payment flows\n`;
            } else {
                guidance += `1. Review the policy rules in ${this.getPolicyFileLocation()}\n`;
                guidance += `2. Adjust your changes to comply\n`;
                guidance += `3. Or request an exception from the policy owner\n`;
            }
            
            if (decision.effectiveMode === 'enforce') {
                guidance += `\nNote: Enforce mode cannot be overridden locally.\n`;
                guidance += `Contact your administrator for exceptions.\n`;
            }
        } else if (decision.action === 'prompt') {
            guidance += `• Type 'y' to proceed with the action\n`;
            guidance += `• Type 'n' to cancel\n`;
            guidance += `• Or use --force flag to skip confirmation\n`;
        }
        
        return guidance;
    }
    
    getPolicyFileLocation(source) {
        if (!source) return './delimit.yml';
        if (source === 'project policy') return './delimit.yml';
        if (source === 'user policy') return '~/.config/delimit/delimit.yml';
        if (source === 'system policy') return '/etc/delimit/delimit.yml';
        if (source === 'org policy') return 'Organization policy (contact admin)';
        if (source === 'defaults') return 'Built-in defaults';
        // Handle rule-specific sources with paths
        if (source && source._sourcePath) return source._sourcePath;
        return source;
    }
    
    isStrongerMode(mode1, mode2) {
        const strength = { advisory: 1, guarded: 2, enforce: 3 };
        return strength[mode1] > strength[mode2];
    }
    
    matchesPattern(str, pattern) {
        // Simple glob matching - in production use minimatch
        const regex = pattern
            .replace(/\*/g, '.*')
            .replace(/\?/g, '.');
        return new RegExp(`^${regex}$`).test(str);
    }
    
    getPolicySources(policies) {
        const sources = [];
        if (policies._sources) {
            if (policies._sources.org) sources.push('org');
            if (policies._sources.user) sources.push('user');
            if (policies._sources.project) sources.push('project');
        }
        return sources.length > 0 ? sources : ['local'];
    }
    
    findPolicySource(policies, field) {
        // Handle the new policy structure with defaultModeSource
        if (field === 'defaultMode' && policies.defaultModeSource) {
            return policies.defaultModeSource;
        }
        if (policies._sources && policies._sources[field]) {
            return policies._sources[field];
        }
        return 'defaults';
    }
    
    generateId() {
        return Date.now().toString(36) + Math.random().toString(36).substr(2);
    }
}

module.exports = DecisionEngine;