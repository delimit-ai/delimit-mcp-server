#!/bin/bash
echo "Testing Delimit hooks..."

# Test pre-bash hook
echo "Testing bash hook..."
node /home/delimit/npm-delimit/hooks/pre-bash-hook.js '{"command":"ls"}'

# Test pre-write hook
echo "Testing write hook..."
node /home/delimit/npm-delimit/hooks/pre-write-hook.js '{"file_path":"/tmp/test.txt"}'

echo "✓ Hook tests complete"
