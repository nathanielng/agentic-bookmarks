"""Local AI (PLAN05 §11, CG6): an LLM proposes the category list, a decision model assigns categories.

Both run on this Mac from the Hugging Face cache; nothing is sent anywhere. Every result is a changeset
that changes nothing until approved.
"""


class AINotReady(RuntimeError):
    """A model, runtime or package is missing; the message says how to get it."""
