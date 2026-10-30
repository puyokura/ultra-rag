# Security

## Reporting

Please report security issues privately through GitHub Security Advisories:

https://github.com/puyokura/ultra-rag/security/advisories/new

Do not open a public issue containing a key, password, token, private key, or personal data.

## Scope

ultra-RAG is a local-first tool. The key vault protects values at rest, but any command that receives a selected key can read it. Redaction reduces accidental output; it does not prevent a program from transforming or transmitting a value.

The HTTP API is intended for a private network or a TLS reverse proxy. Do not expose it directly to the internet.
