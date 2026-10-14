"""Name freeform text by its producer without changing runtime strings.

These markers make trust and disclosure questions visible in signatures and
reviews. They deliberately perform no validation or sanitization: the boundary
that receives the text still owns its structural limits, and every eventual
log, wire, or display sink still owns whether that provenance is acceptable.
"""

from typing import NewType


# Caller excerpts need contextual redaction and safe text rendering before
# diagnostic publication; scientific values are not automatically sensitive.
UserProvidedText = NewType("UserProvidedText", str)

# Model prose may hallucinate or reproduce user text from its prompt. A valid
# model protocol response therefore remains distinct from reviewed product copy.
ModelGeneratedText = NewType("ModelGeneratedText", str)

# Provider diagnostics include attributed external explanations after redaction.
# Publication rules own their bounds; this marker performs no sanitization.
ProviderDiagnosticText = NewType("ProviderDiagnosticText", str)

# Server A authors problem details for its authenticated caller. They may quote
# request facts and are suitable for bounded operator diagnostics, not for
# automatic publication to another audience.
ApiDiagnosticText = NewType("ApiDiagnosticText", str)
