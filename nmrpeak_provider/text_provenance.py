"""Name freeform text by its producer without changing runtime strings.

These markers make trust and disclosure questions visible in signatures and
reviews. They deliberately perform no validation or sanitization: the boundary
that receives the text still owns its structural limits, and every eventual
log, wire, or display sink still owns whether that provenance is acceptable.
"""

from typing import NewType


# Caller text may contain private scientific material or hostile presentation
# content. Verification of its bytes and identity does not make it publishable.
UserProvidedText = NewType("UserProvidedText", str)

# Model prose may hallucinate or reproduce user text from its prompt. A valid
# model protocol response therefore remains distinct from reviewed product copy.
ModelGeneratedText = NewType("ModelGeneratedText", str)

# Server A authors problem details for its authenticated caller. They may quote
# request facts and are suitable for bounded operator diagnostics, not for
# automatic publication to another audience.
ApiDiagnosticText = NewType("ApiDiagnosticText", str)
