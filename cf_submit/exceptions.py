class CFError(Exception):
    """An error suitable for the CLI."""


class ConfigError(CFError):
    pass


class AuthenticationError(CFError):
    pass


class VerificationRequired(AuthenticationError):
    pass


class ContestNotFound(CFError):
    pass


class GymNotFound(CFError):
    pass


class ProblemNotFound(CFError):
    pass


class FileNotFound(CFError):
    pass


class InvalidFilename(CFError):
    pass


class CacheNotFound(CFError):
    pass


class LanguageNotFound(CFError):
    pass


class PageStructureChanged(CFError):
    pass


class CSRFTokenError(PageStructureChanged):
    pass


class SubmissionRejected(CFError):
    pass


class NetworkError(CFError):
    pass


class SubmissionUnknown(CFError):
    def __init__(self, recent_url: str, reason: str = ""):
        message = f"Submission Status Unknown. Check your submissions: {recent_url}"
        super().__init__(f"{message} {reason}" if reason else message)
