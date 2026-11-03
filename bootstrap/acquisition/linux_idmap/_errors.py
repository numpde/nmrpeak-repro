"""Failures reported by process and namespace checks."""


class IdentityError(ValueError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category
