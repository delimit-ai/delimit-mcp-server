#!/usr/bin/env node

// Test script to simulate a Git pre-commit hook
const axios = require('axios');

async function testHook() {
    const context = {
        command: 'pre-commit',
        pwd: '/home/delimit/npm-delimit',
        gitBranch: 'main',
        files: ['lib/payment/stripe.js', 'README.md'],
        diff: 'diff --git a/lib/payment/stripe.js\n+const stripe = require("stripe");'
    };
    
    try {
        const response = await axios.post('http://127.0.0.1:7823/evaluate', context);
        console.log('Decision:', response.data);
        
        // Now test the explain endpoint
        const explainResponse = await axios.get('http://127.0.0.1:7823/explain/last');
        console.log('\n' + explainResponse.data.explanation);
    } catch (e) {
        console.error('Error:', e.message);
    }
}

testHook();