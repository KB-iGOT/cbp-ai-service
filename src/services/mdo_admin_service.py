"""
Service for fetching MDO (Ministry/Department/Organization) admin and leader information
from the iGOT Karmayogi portal.
"""
import httpx
from typing import List, Dict, Any, Optional
from fastapi import HTTPException, status

from ..core.configs import settings
from ..core.logger import logger


class MDOAdminService:
    """Service for interacting with iGOT portal to fetch MDO admin/leader data"""
    
    def __init__(self, auth_token: str = None):
        self.auth_token = auth_token or settings.KB_AUTH_TOKEN
        self.base_url = settings.KB_BASE_URL
        self.timeout = 30.0
    
    async def get_mdo_admins(self, department_id: str) -> List[Dict[str, Any]]:
        """
        Fetch MDO admins and leaders from iGOT portal for a specific department.
        
        Args:
            department_id: The department (rootOrgId) to filter MDO admins by
            
        Returns:
            List of dictionaries containing MDO admin details:
                - id (str): User ID
                - firstName (str): First name
                - lastName (str): Last name
                - rootOrgId (str): Root organization ID
                - organisations (list): List of organization objects
                - roles (list): List of role strings
                
        Raises:
            HTTPException: If the API call fails
        """
        try:
            url = f"{self.base_url}/api/private/user/v1/search"
            
            # Format token properly - ensure it has 'Bearer ' prefix if needed or handles 'bearer ' prefix
            auth_header = self.auth_token
            if not auth_header.lower().startswith("bearer "):
                auth_header = f"Bearer {auth_header}"
                
            headers = {
                "Content-Type": "application/json",
                "Authorization": auth_header
            }
            
            payload = {
                "request": {
                    "filters": {
                        "organisations.roles": ["MDO_LEADER", "MDO_ADMIN"],
                        "rootOrgId": department_id
                    },
                    "fields": ["firstName", "lastName", "id", "rootOrgId", "organisations", "roles"]
                }
            }
            
            logger.info(f"Fetching MDO admins for department: {department_id} from {url}")
            
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    json=payload,
                    headers=headers
                )
                
                response.raise_for_status()
                data = response.json()
                
                # Extract users from response
                users = data.get("result", {}).get("response", {}).get("content", [])
                
                # Process the raw users into a structured dictionary array
                processed_users: List[Dict[str, Any]] = []
                for user in users:
                    processed_users.append({
                        "id": user.get("id", ""),
                        "firstName": user.get("firstName", ""),
                        "lastName": user.get("lastName", ""),
                        "rootOrgId": user.get("rootOrgId", ""),
                        "organisations": user.get("organisations", []),
                        "roles": user.get("roles", [])
                    })
                
                logger.info(f"Found {len(processed_users)} MDO admins/leaders")
                logger.info(f"roles of all the users: {[user['roles'] for user in processed_users]}")
                
                return processed_users
                
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error fetching MDO admins: {e.response.status_code} - {e.response.text}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to fetch MDO admins from iGOT portal: {str(e)}"
            )
        except httpx.RequestError as e:
            logger.error(f"Request error fetching MDO admins: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Unable to connect to iGOT portal: {str(e)}"
            )
        except Exception as e:
            logger.error(f"Unexpected error fetching MDO admins: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch MDO admins: {str(e)}"
            )


# Initialize the service
mdo_admin_service = MDOAdminService()
