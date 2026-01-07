"""Email domain model"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class Email:
    """
    Represents an email message with all relevant metadata.
    """
    
    id: str
    subject: str
    sender: str
    sender_name: Optional[str] = None
    sender_email: Optional[str] = None
    body: str = ""
    snippet: str = ""
    received_at: Optional[datetime] = None
    is_read: bool = False
    labels: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    
    def __post_init__(self):
        """Extract sender name and email after initialization"""
        if not self.sender_name or not self.sender_email:
            self._parse_sender()
    
    def _parse_sender(self):
        """Parse sender string "Name <email@domain.com>" into components"""
        if not self.sender:
            return
            
        if '<' in self.sender and '>' in self.sender:
            parts = self.sender.split('<')
            self.sender_name = parts[0].strip().strip('"')
            self.sender_email = parts[1].strip('>')
        else:
            self.sender_name = self.sender
            self.sender_email = self.sender
