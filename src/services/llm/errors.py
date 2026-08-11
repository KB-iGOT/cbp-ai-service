class LLMError(Exception):
    """Base class for all LLM adapter errors."""


class LLMRateLimitError(LLMError):
    pass


class LLMTransientError(LLMError):
    pass


class LLMBadRequestError(LLMError):
    pass


class LLMEmptyResponseError(LLMError):
    pass


class LLMSchemaError(LLMError):
    pass
