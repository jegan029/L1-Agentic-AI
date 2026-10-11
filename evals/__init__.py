"""Evaluation suite for model use (ADR 0009).

    python -m evals.run            every corpus against the stand-in providers (runs in CI)
    python -m evals.run --live     the same against the provider in the environment (costs money)

Three corpora, each a JSON file in this directory:

golden.json     incidents with the terminal outcome and the capability calls expected of them
injection.json  hostile text for ticket fields, tool output and articles
redaction.json  secrets and personal data that must never appear in a provider request

What is measured is what the system did, not what a model said: capability
calls are recorded inside each connector's `invoke`, and provider requests are
captured as the bytes that reached an HTTP server.
"""
