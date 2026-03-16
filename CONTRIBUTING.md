# Contributing to Delimit

Thank you for your interest in contributing to Delimit. We welcome contributions from the community.

## How to Contribute

### Reporting Issues

1. Check if the issue already exists
2. Create a new issue with:
   - Clear title and description
   - Steps to reproduce
   - Expected vs actual behavior
   - Environment details (OS, Node version, etc.)

### Submitting Pull Requests

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Make your changes
4. Run the test suite
5. Commit with clear messages
6. Push to your fork
7. Open a PR with:
   - Description of changes
   - Related issue numbers
   - Test results

## Development Setup

### CLI (npm)

```bash
npm install -g delimit-cli
delimit doctor
```

### GitHub Action

See [delimit-action](https://github.com/delimit-ai/delimit-action) for CI integration.

## Code Style

- Follow existing conventions in the codebase
- Use type hints where appropriate
- Document functions and classes
- Keep functions focused and small

## Testing

All PRs must:
- Pass existing tests
- Include tests for new features
- Not introduce regressions

## Areas for Contribution

- Documentation improvements
- Bug fixes
- New governance rules and policy presets
- Performance improvements
- Framework integrations (Zero-Spec extractors)

## Questions?

- Open a [Discussion](https://github.com/delimit-ai/delimit/discussions) on GitHub
- Email opensource@delimit.ai
