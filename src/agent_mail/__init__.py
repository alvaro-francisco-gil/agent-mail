"""Read-only access to a personal mailbox, exposed over MCP.

Nothing in this package can send, delete, or modify mail. That is a property of
what is absent, not of a flag: no write path is implemented, and the OAuth
scopes requested are read-only.
"""
