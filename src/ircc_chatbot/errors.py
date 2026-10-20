"""Errors raised by the chatbot.

What: one exception type per kind of failure, all under ChatbotError:

    ChatbotError
    ├── MetadataError          bad metadata file            -> refuse to start
    ├── IntentError            intent breaks the contract   -> tell the user / retry the LLM
    │   └── IdentifierError    identifier can't be resolved
    └── IntentExtractionError  LLM reply unusable or LLM unreachable

How: the classes add no behaviour; their type is the information. `except
IntentError` also catches IdentifierError, and `except ChatbotError` catches
everything this project raises but not real bugs (TypeError etc.).

Why: each kind of failure needs a different response, and the caller should not
have to read error text to tell them apart. Every message says what was wrong
and, where it helps, what is allowed, so it can be shown to an analyst as is.
"""


class ChatbotError(Exception):
    """Base class for all chatbot errors. Catch this to handle every expected failure in one place."""


class MetadataError(ChatbotError):
    """A metadata file is missing or invalid. The application must not start.

    Raised by metadata_loader.py / metadata_checks.py, always naming the file and field.
    """


class IntentError(ChatbotError):
    """An intent does not follow the intent contract. No SQL is built.

    Raised by intent.py, e.g. an unknown stereotype, attribute or operator.
    """


class IdentifierError(IntentError):
    """An object identifier (e.g. 'SREQ 12a') cannot be resolved.

    Raised by identifier_resolver.py. A subclass of IntentError, because a bad
    identifier is one way an intent can be invalid.
    """


class IntentExtractionError(ChatbotError):
    """The LLM's reply could not be turned into a valid intent.

    Raised by intent_extraction.py and ollama_client.py: empty or non-JSON reply,
    timeout, Ollama not running, unknown model.
    """
