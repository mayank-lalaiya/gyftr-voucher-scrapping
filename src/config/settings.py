"""
Configuration settings using Singleton pattern.
Manages environment variables and application settings.
"""

import os
from typing import Optional


class Settings:
    """
    Singleton configuration class.
    Provides centralized access to environment variables and settings.
    """
    
    _instance: Optional['Settings'] = None
    
    def __new__(cls):
        """Ensure only one instance exists (Singleton pattern)"""
        if cls._instance is None:
            cls._instance = super(Settings, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialize settings from environment variables"""
        if self._initialized:
            return
        
        # Gmail OAuth credentials
        self.client_id = os.environ.get('CLIENT_ID', '')
        self.client_secret = os.environ.get('CLIENT_SECRET', '')
        self.refresh_token = os.environ.get('REFRESH_TOKEN', '')
        
        # GyFTR Specific Configuration
        self.gyftr_spreadsheet_id = os.environ.get('GYFTR_SPREADSHEET_ID')
        if not self.gyftr_spreadsheet_id:
             # This will be caught by factory validation, but good to be explicit
             pass


        # Credentials Dictionary for Gmail Service
        self.gmail_credentials = {
            'token': None,  # Will be refreshed using refresh_token
            'refresh_token': self.refresh_token,
            'token_uri': 'https://oauth2.googleapis.com/token',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'scopes': [
                'https://www.googleapis.com/auth/gmail.readonly', 
                'https://www.googleapis.com/auth/gmail.modify',
                'https://www.googleapis.com/auth/spreadsheets'
            ]
        }

        
        self.force_ipv4 = os.environ.get('FORCE_IPV4', 'true').lower() == 'true'

        self._initialized = True

    def validate(self) -> tuple[bool, list[str]]:
        """Validate required settings are present"""
        missing = []
        if not self.client_id:
            missing.append('CLIENT_ID')
        if not self.client_secret:
            missing.append('CLIENT_SECRET')
        if not self.refresh_token:
            missing.append('REFRESH_TOKEN')
            
        return len(missing) == 0, missing
