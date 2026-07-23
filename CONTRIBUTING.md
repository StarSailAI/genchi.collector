# Contributing

Create a focused branch, add tests for behavioral changes, and run:

```bash
make install
make test
make lint
```

Public SDK and HTTP protocol changes require documentation and a compatibility
note. New built-in Fetchers must use stable external IDs, typed failure semantics
and the shared HTTP safety layer.
