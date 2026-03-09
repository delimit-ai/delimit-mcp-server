# Delimit - Unavoidable AI Governance Layer

## What is Delimit?

Delimit makes AI governance unavoidable. Once installed, it automatically:
- 🛡️ Intercepts ALL AI tool commands (Claude, Gemini, Copilot, etc.)
- 🔒 Validates EVERY Git commit and push
- 📊 Records evidence of all AI-assisted development
- 🚫 Makes ungoverned development impossible

## Installation

```bash
npm install -g delimit
```

**Warning**: Installation permanently modifies your development environment. Governance becomes mandatory.

## What Happens After Installation?

1. **PATH Hijacking**: AI tools are wrapped with governance
2. **Git Hooks**: All commits/pushes are validated globally
3. **Evidence Collection**: All actions are logged
4. **Automatic Activation**: Governance starts with every shell

## How It Works

When you use AI tools:
```bash
$ claude "write a function"
[Delimit] AI GOVERNANCE ACTIVE: CLAUDE
[Delimit] Timestamp: 2024-03-07T10:30:00Z
[Delimit] Command logged and validated
... (claude output) ...
[Delimit] GOVERNANCE COMPLETE: Session recorded
```

When you commit code:
```bash
$ git commit -m "Add feature"
[Delimit] Running pre-commit governance checks...
[Delimit] ✓ Security scan passed
[Delimit] ✓ No exposed secrets detected
[Delimit] ✓ All governance checks passed
```

## Features

- **Zero Configuration**: Works immediately after installation
- **Universal Coverage**: Governs ALL development tools
- **Tamper Resistant**: Difficult to bypass without detection
- **Evidence Trail**: Complete audit log of all activities
- **Developer Friendly**: Minimal friction, maximum security

## Compliance

Delimit helps you meet compliance requirements:
- SOC2 Type II
- ISO 27001
- GDPR Article 25
- NIST Cybersecurity Framework

## The Infection Pattern

Delimit uses an "infection" pattern - once installed on a machine, it spreads to:
- Every Git repository
- Every terminal session
- Every AI tool interaction
- Every code commit

This ensures 100% governance coverage with no gaps.

## Status Check

```bash
delimit status
```

Shows:
- ✓ PATH Hijack: ACTIVE
- ✓ Git Hooks: ACTIVE  
- ✓ AI Tool Shims: X installed
- ✓ Evidence Collection: ACTIVE

## Uninstallation

Uninstalling Delimit requires manual system changes:
1. Remove PATH modifications from shell profiles
2. Unset Git global hooks
3. Delete ~/.delimit directory
4. Restart all terminals

This is intentionally difficult to ensure governance persistence.

## License

MIT License - Use at your own risk. Governance is not optional.

## Support

- Web: https://delimit.ai
- Email: support@delimit.ai
- GitHub: https://github.com/delimit-ai/delimit-cli

---

**Remember**: Once installed, Delimit cannot be easily removed. This is by design.