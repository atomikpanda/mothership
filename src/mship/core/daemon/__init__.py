"""The Mothership daemon (#470): one supervised host control-plane process per
OS user per host, shipped from the same package as the CLI.

v1 is deliberately a minimal shell — singleton lease, rotated logs, start
history, unix control socket. It serves no workspaces (#472), opens no tunnel
(#471), and supervises no workers (#473); those arrive behind the capability
seams reported by the control app.
"""
