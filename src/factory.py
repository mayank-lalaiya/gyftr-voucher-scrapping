"""
Service factory for dependency injection.
Implements Factory pattern for creating service instances.
"""

from src.config import Settings
from src.repositories import GmailRepository
from src.services import GyftrProcessingService


class ServiceFactory:
    """
    Factory for creating service instances with proper dependencies.
    Implements Dependency Injection pattern.
    """
    
    _settings = None
    _gmail_repository = None
    _gyftr_processing_service = None
    
    @property
    def settings(self) -> Settings:
        """Get or create Settings singleton"""
        if self._settings is None:
            self._settings = Settings()
        return self._settings

    def get_gmail_repository(self) -> GmailRepository:
        """
        Get or create Gmail repository instance.
        
        Returns:
            GmailRepository instance
        """
        if self._gmail_repository is None:
            self._gmail_repository = GmailRepository(self.settings)
        return self._gmail_repository
    
    def get_gyftr_processing_service(self) -> GyftrProcessingService:
        """
        Get or create GyFTR processing service instance.
        
        Returns:
            GyftrProcessingService instance with dependencies injected
        """
        if self._gyftr_processing_service is None:
            gmail_repo = self.get_gmail_repository()
            self._gyftr_processing_service = GyftrProcessingService(
                gmail_repository=gmail_repo,
                settings=self.settings
            )
        return self._gyftr_processing_service
    
    def validate_configuration(self) -> bool:
        """
        Validate that all required configuration is present.
        
        Returns:
            True if valid, raises ValueError if invalid
        """
        is_valid, missing = self.settings.validate()
        if not is_valid:
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")
        return True
