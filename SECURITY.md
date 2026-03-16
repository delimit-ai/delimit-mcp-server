# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 2.x     | Yes       |
| 1.x     | No        |

## Reporting a Vulnerability

We take security seriously at Delimit. If you discover a security vulnerability, please follow these steps:

1. **Do NOT** create a public GitHub issue
2. Email security@delimit.ai with:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Your suggested fix (if any)

## Response Timeline

- **Acknowledgment**: Within 24 hours
- **Initial Assessment**: Within 72 hours
- **Fix Timeline**: Based on severity
  - Critical: Within 7 days
  - High: Within 14 days
  - Medium: Within 30 days
  - Low: Next release

## Security Best Practices

When using Delimit:

1. **Never commit API keys or tokens** to your repository
2. **Use environment variables** for sensitive configuration
3. **Keep Delimit updated** to the latest version
4. **Review PR annotations** before merging

## Data Privacy

Delimit processes your API specifications locally. The CLI and GitHub Action do not send your specs to external servers.
