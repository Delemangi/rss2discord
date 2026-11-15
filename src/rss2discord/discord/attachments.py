"""Attachment payloads shared by message rendering and request posting."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReportFile:
    """One uploaded report attachment referenced from the message payload."""

    filename: str
    content: bytes
    content_type: str
