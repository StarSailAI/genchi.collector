# Security

- Controller admin APIs fail closed when `CONTROL_API_TOKEN` is absent.
- Enrollment tokens expire, have bounded uses, and are stored as hashes.
- Worker credentials are stored as hashes and can be revoked.
- Use HTTPS for every non-local Controller connection.
- Source snapshots contain secret references, never secret values.
- Dashboard calls the Controller server-side and does not expose the token to
  browser JavaScript.
- Built-in HTTP fetchers block loopback, private, link-local, multicast and
  reserved addresses unless explicitly allowed.
- Redirect targets are validated again.
- Response bytes, retries, redirects, robots and per-host request rates are
  bounded.

Installed plugins are trusted code. Review plugin packages and pin versions in
Worker images. Child-process isolation protects Worker availability but is not a
security boundary.

Report vulnerabilities privately as described in the repository SECURITY file.
