"""Headless application layer (DESIGN.md §11).

Everything the terminal UI does goes through these functions, which return plain
dataclasses / dicts and never import Typer or Rich. A future web dashboard can
call the same functions without touching the engine or the CLI.
"""
