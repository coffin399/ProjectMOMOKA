# MOMOKA/tracker/error/errors.py
"""
Custom error classes for Tracker Bot
"""

class R6BotError(Exception):
    """Base class for all R6 Bot related errors"""
    pass


class R6APIError(R6BotError):
    """Error from R6 API response"""
    def __init__(self, status_code_or_message):
        if isinstance(status_code_or_message, int):
            self.status_code = status_code_or_message
            self.message = f"R6 API returned status code {status_code_or_message}"
        else:
            self.status_code = None
            self.message = str(status_code_or_message)
        super().__init__(self.message)


class PlayerNotFoundError(R6BotError):
    """Error when player is not found"""
    def __init__(self, username: str, platform: str):
        self.username = username
        self.platform = platform
        self.message = f"Player '{username}' not found on platform '{platform}'"
        super().__init__(self.message)


class InvalidPlatformError(R6BotError):
    """Error when an invalid platform is specified"""
    def __init__(self, platform: str, valid_platforms: list):
        self.platform = platform
        self.valid_platforms = valid_platforms
        self.message = f"Invalid platform '{platform}'. Valid platforms: {', '.join(valid_platforms)}"
        super().__init__(self.message)


class RateLimitError(R6BotError):
    """Error when API rate limit is reached"""
    def __init__(self, retry_after: int = None):
        self.retry_after = retry_after
        if retry_after:
            self.message = f"Rate limit exceeded. Please try again after {retry_after} seconds"
        else:
            self.message = "Rate limit exceeded. Please try again later"
        super().__init__(self.message)


class DataParseError(R6BotError):
    """Error when API response parsing fails"""
    def __init__(self, details: str = None):
        self.details = details
        self.message = f"Failed to parse API response data{': ' + details if details else ''}"
        super().__init__(self.message)


class NetworkError(R6BotError):
    """Network connection error"""
    def __init__(self, details: str = None):
        self.details = details
        self.message = f"Network connection error{': ' + details if details else ''}"
        super().__init__(self.message)


class TimeoutError(R6BotError):
    """Request timeout error"""
    def __init__(self, timeout_seconds: int = None):
        self.timeout_seconds = timeout_seconds
        if timeout_seconds:
            self.message = f"Request timed out after {timeout_seconds} seconds"
        else:
            self.message = "Request timed out"
        super().__init__(self.message)


class OperatorNotFoundError(R6BotError):
    """Error when operator is not found"""
    def __init__(self, operator_name: str):
        self.operator_name = operator_name
        self.message = f"Operator '{operator_name}' not found"
        super().__init__(self.message)


class StatsNotAvailableError(R6BotError):
    """Error when player statistics are not available"""
    def __init__(self, username: str, reason: str = None):
        self.username = username
        self.reason = reason
        if reason:
            self.message = f"Statistics for player '{username}' are not available: {reason}"
        else:
            self.message = f"Statistics for player '{username}' are not available. The account may be private or has no statistics."
        super().__init__(self.message)


class ServerStatusError(R6BotError):
    """Error when server status cannot be retrieved"""
    def __init__(self, details: str = None):
        self.details = details
        self.message = f"Failed to retrieve server status{': ' + details if details else ''}"
        super().__init__(self.message)


# ==================== Valorant Errors ====================

class ValorantBotError(Exception):
    """Base class for all Valorant Bot related errors"""
    pass


class ValorantAPIError(ValorantBotError):
    """Error from Valorant API response"""
    def __init__(self, status_code_or_message):
        if isinstance(status_code_or_message, int):
            self.status_code = status_code_or_message
            self.message = f"Valorant API returned status code {status_code_or_message}"
        else:
            self.status_code = None
            self.message = str(status_code_or_message)
        super().__init__(self.message)


class ValorantPlayerNotFoundError(ValorantBotError):
    """Error when Valorant player is not found"""
    def __init__(self, name: str, tag: str, region: str = None):
        self.name = name
        self.tag = tag
        self.region = region
        if region:
            self.message = f"Player '{name}#{tag}' not found in region '{region}'"
        else:
            self.message = f"Player '{name}#{tag}' not found"
        super().__init__(self.message)


class InvalidRegionError(ValorantBotError):
    """Error when an invalid region is specified"""
    def __init__(self, region: str, valid_regions: list):
        self.region = region
        self.valid_regions = valid_regions
        self.message = f"Invalid region '{region}'. Valid regions: {', '.join(valid_regions)}"
        super().__init__(self.message)


class ValorantRateLimitError(ValorantBotError):
    """Error when Valorant API rate limit is reached"""
    def __init__(self, retry_after: int = None):
        self.retry_after = retry_after
        if retry_after:
            self.message = f"Valorant API rate limit exceeded. Please try again after {retry_after} seconds"
        else:
            self.message = "Valorant API rate limit exceeded. Please try again later"
        super().__init__(self.message)


class ValorantDataParseError(ValorantBotError):
    """Error when Valorant API response parsing fails"""
    def __init__(self, details: str = None):
        self.details = details
        self.message = f"Failed to parse Valorant API response{': ' + details if details else ''}"
        super().__init__(self.message)


class ValorantNetworkError(ValorantBotError):
    """Network connection error for Valorant API"""
    def __init__(self, details: str = None):
        self.details = details
        self.message = f"Network connection error{': ' + details if details else ''}"
        super().__init__(self.message)


class ValorantStatsNotAvailableError(ValorantBotError):
    """Error when Valorant player statistics are not available"""
    def __init__(self, name: str, tag: str, reason: str = None):
        self.name = name
        self.tag = tag
        self.reason = reason
        if reason:
            self.message = f"Statistics for player '{name}#{tag}' are not available: {reason}"
        else:
            self.message = f"Statistics for player '{name}#{tag}' are not available. The account may be private or has no competitive matches."
        super().__init__(self.message)


class AgentNotFoundError(ValorantBotError):
    """Error when agent is not found"""
    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self.message = f"Agent '{agent_name}' not found"
        super().__init__(self.message)


class InvalidModeError(ValorantBotError):
    """Error when an invalid game mode is specified"""
    def __init__(self, mode: str, valid_modes: list):
        self.mode = mode
        self.valid_modes = valid_modes
        self.message = f"Invalid mode '{mode}'. Valid modes: {', '.join(valid_modes)}"
        super().__init__(self.message)