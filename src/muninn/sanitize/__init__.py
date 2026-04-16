"""URL sanitization — leakage-critical, see SPEC.md §"Sanitization rules"."""

from muninn.sanitize.url import SanitizationResult, sanitize_url

__all__ = ["SanitizationResult", "sanitize_url"]
