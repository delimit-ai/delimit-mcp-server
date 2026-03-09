#!/usr/bin/env node

const DecisionEngine = require('./lib/decision-engine');
const axios = require('axios');

async function runTests() {
    console.log('=== DECISION ENGINE HOSTILE VERIFICATION ===\n');
    
    // First restore good policy and restart agent
    const fs = require('fs');
    const goodPolicy = `defaultMode: advisory

rules:
  - name: "Production Protection"
    mode: enforce
    triggers:
      - gitBranch: [main, master, production]
      
  - name: "Payment Code Security"
    mode: enforce
    triggers:
      - path: "**/payment/**"
      - content: ["stripe", "payment", "billing"]
      
  - name: "Documentation Freedom"
    mode: advisory
    triggers:
      - path: "**/*.md"
    final: true`;
    
    fs.writeFileSync('delimit.yml', goodPolicy);
    
    // Kill existing agent
    try {
        require('child_process').execSync('pkill -f "node lib/agent.js"');
    } catch(e) {}
    
    // Start fresh agent
    const agent = require('child_process').spawn('node', ['lib/agent.js'], {
        detached: true,
        stdio: 'ignore'
    });
    agent.unref();
    
    // Wait for agent
    await new Promise(r => setTimeout(r, 2000));
    
    const tests = [
        {
            name: 'TEST 1: Documentation file -> Advisory',
            context: {
                command: 'pre-commit',
                pwd: '/test',
                gitBranch: 'feature',
                files: ['README.md', 'docs/api.md'],
                diff: 'documentation changes'
            },
            expected: 'advisory'
        },
        {
            name: 'TEST 2: Payment path -> Enforce',
            context: {
                command: 'pre-commit', 
                pwd: '/test',
                gitBranch: 'feature',
                files: ['lib/payment/stripe.js'],
                diff: 'payment code changes'
            },
            expected: 'enforce'
        },
        {
            name: 'TEST 3: Main branch -> Enforce',
            context: {
                command: 'pre-commit',
                pwd: '/test',
                gitBranch: 'main',
                files: ['lib/utils.js'],
                diff: 'utility changes'
            },
            expected: 'enforce'
        },
        {
            name: 'TEST 4: No match -> Default advisory',
            context: {
                command: 'pre-commit',
                pwd: '/test',
                gitBranch: 'feature',
                files: ['lib/utils.js'],
                diff: 'regular code'
            },
            expected: 'advisory'
        },
        {
            name: 'TEST 5: Conflicting rules -> Stronger wins',
            context: {
                command: 'pre-commit',
                pwd: '/test',
                gitBranch: 'main',
                files: ['README.md'],
                diff: 'readme on main branch'
            },
            expected: 'enforce' // Production Protection should win over Documentation Freedom
        },
        {
            name: 'TEST 6: Determinism check (repeat test 2)',
            context: {
                command: 'pre-commit',
                pwd: '/test',
                gitBranch: 'feature',
                files: ['lib/payment/stripe.js'],
                diff: 'payment code changes'
            },
            expected: 'enforce'
        }
    ];
    
    const results = [];
    for (const test of tests) {
        try {
            const response = await axios.post('http://127.0.0.1:7823/evaluate', test.context);
            const decision = response.data;
            
            const result = {
                test: test.name,
                expected: test.expected,
                actual: decision.mode,
                action: decision.action,
                rule: decision.rule,
                pass: decision.mode === test.expected
            };
            
            results.push(result);
            console.log(`${test.name}`);
            console.log(`  Expected: ${test.expected}, Actual: ${decision.mode}`);
            console.log(`  Rule: ${decision.rule || 'none'}`);
            console.log(`  Status: ${result.pass ? '✅ PASS' : '❌ FAIL'}\n`);
            
            // Get explanation for this decision
            const explainResponse = await axios.get('http://127.0.0.1:7823/explain/last');
            if (explainResponse.data.explanation) {
                console.log('  Explanation quality check:');
                const exp = explainResponse.data.explanation;
                console.log(`    - Has decision ID: ${exp.includes('Decision ID:') ? '✓' : '✗'}`);
                console.log(`    - Has effective mode: ${exp.includes('Effective:') ? '✓' : '✗'}`);
                console.log(`    - Has matched rules: ${exp.includes('MATCHED RULES') || exp.includes('No matching rules') ? '✓' : '✗'}`);
                console.log(`    - Has context: ${exp.includes('CONTEXT') ? '✓' : '✗'}\n`);
            }
            
        } catch (e) {
            results.push({
                test: test.name,
                expected: test.expected,
                actual: 'ERROR',
                error: e.message,
                pass: false
            });
            console.log(`${test.name}: ❌ ERROR - ${e.message}\n`);
        }
    }
    
    // Summary
    console.log('=== SUMMARY ===');
    const passed = results.filter(r => r.pass).length;
    console.log(`Passed: ${passed}/${results.length}`);
    
    // Check determinism
    if (results[1].actual === results[5].actual && results[1].rule === results[5].rule) {
        console.log('✅ DETERMINISM CHECK: Same input produced same output');
    } else {
        console.log('❌ DETERMINISM CHECK: Same input produced different outputs!');
    }
    
    // Kill agent
    try {
        require('child_process').execSync('pkill -f "node lib/agent.js"');
    } catch(e) {}
    
    process.exit(passed === results.length ? 0 : 1);
}

runTests().catch(console.error);