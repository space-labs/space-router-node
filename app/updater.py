import requests
import logging
from app.version import __version__

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com/repos/gluwa/space-router-node/releases/latest"

def check_for_updates():
    try:
        response = requests.get(GITHUB_API_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        latest_version = data['tag_name'].lstrip('v')
        if latest_version != __version__:
            logger.info(f"Update available: {latest_version} (Current: {__version__})")
            return data['assets']
        return None
    except Exception as e:
        logger.error(f"Failed to check for updates: {e}")
        return None
