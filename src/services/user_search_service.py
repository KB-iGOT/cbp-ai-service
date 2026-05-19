"""
Service for searching users from the iGOT Karmayogi portal.
"""
import httpx
from typing import List, Dict, Any, Optional
from fastapi import HTTPException, status

from ..core.configs import settings
from ..core.logger import logger


class UserSearchService:
    """Service for searching users from iGOT portal"""
    
    def __init__(self, auth_token: str = None):
        self.auth_token = auth_token or settings.KB_AUTH_TOKEN
        self.base_url = settings.KB_BASE_URL
        self.timeout = 30.0
    
    async def search_users(self, body: dict) -> List[Dict[str, Any]]:
        """
        Search users from iGOT portal based on provided filters.
        
        Args:
            body: The request body to filter users by
            
        Returns:
            List of dictionaries containing MDO admin details:
                - id (str): User ID
                - firstName (str): First name
                - lastName (str): Last name
                - rootOrgId (str): Root organization ID
                - organisations (list): List of organization objects
                
        Raises:
            HTTPException: If the API call fails
        """
        try:
            url = f"{self.base_url}/api/private/user/v1/search"
            
            # Format token properly - ensure it has 'Bearer ' prefix if needed or handles 'bearer ' prefix
            auth_header = self.auth_token
                
            headers = {
                "Content-Type": "application/json",
                "Authorization": auth_header
            }
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    json=body,
                    headers=headers
                )
                
                response.raise_for_status()
                data = response.json()
                
                # Extract users from response
                users = data.get("result", {}).get("response", {}).get("content", [])
                
                # Process the raw users into a structured dictionary array
                processed_users: List[Dict[str, Any]] = []
                for user in users:
                    # Extract roles from organisations
                    roles = []
                    organisations = user.get("organisations", [])
                    for org in organisations:
                        org_roles = org.get("roles", [])
                        roles.extend(org_roles)
                    
                    # Remove duplicates while preserving order
                    roles = list(dict.fromkeys(roles))
                    
                    processed_users.append({
                        "id": user.get("id", ""),
                        "firstName": user.get("firstName", ""),
                        "lastName": user.get("lastName", ""),
                        "rootOrgId": user.get("rootOrgId", ""),
                        "organisations": user.get("organisations", []),
                        "roles": roles,
                        "profileDetails": user.get("profileDetails", {}),
                    })
                    
                return processed_users
                
        except httpx.HTTPStatusError as e:
            logger.exception("HTTP error fetching MDO admins")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to fetch MDO admins from iGOT portal"
            )
        except httpx.RequestError as e:
            logger.exception("Request error fetching MDO admins")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Failed to fetch MDO admins from iGOT portal"
            )
        except Exception as e:
            logger.exception("Unexpected error fetching MDO admins")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to fetch MDO admins"
            )


# Initialize the service
user_search_service = UserSearchService()
