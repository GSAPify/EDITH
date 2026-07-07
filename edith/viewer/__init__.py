"""Local, offline knowledge-graph viewer for EDITH's memory.

Serves ``MemoryStore.graph_snapshot()`` as force-graph JSON plus a static
page that renders it. Stdlib HTTP only — zero new runtime deps — bound to
127.0.0.1. See ``edith.viewer.__main__`` for the launcher.
"""
