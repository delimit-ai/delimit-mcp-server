#!/bin/bash
echo "Updating Delimit..."
cd /home/delimit/npm-delimit
git pull
npm install
echo "✓ Delimit updated"
