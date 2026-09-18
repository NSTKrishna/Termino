"""Evaluation against simulator ground truth (the only package allowed to read it)."""

from .evaluate import LABEL, evaluate

__all__ = ["LABEL", "evaluate"]
