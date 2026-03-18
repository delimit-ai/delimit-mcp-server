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
