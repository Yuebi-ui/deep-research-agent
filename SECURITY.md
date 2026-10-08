# Security

## Credentials

Do not commit provider keys, tokens, private `.env` files, `config.yml`, local
SQLite databases or Chroma data. Example configuration files intentionally contain
blank or non-sensitive placeholder values.

If a real credential is committed, **rotate/revoke it first**. Removing the text
from the latest commit is not sufficient once it has entered Git history.

## Memory data

Research reports, extracted claims and episodic traces can contain user-provided
or externally retrieved text. Treat persisted memory as potentially sensitive
application data. The current repository does not claim tenant isolation; shared
multi-user deployment needs an explicit tenant/retention design before enabling
cross-task memory.

## Reporting

For a public repository, avoid placing secrets or private research content in a
public issue. Use the repository owner's private contact or GitHub's private
security-advisory flow when available.
