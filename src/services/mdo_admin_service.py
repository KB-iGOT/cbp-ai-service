"""
Service for fetching MDO (Ministry/Department/Organization) admin and leader information
from the iGOT Karmayogi portal.
"""
import httpx
from typing import List, Dict, Optional
from fastapi import HTTPException, status

from ..core.logger import logger


class MDOAdminService:
    """Service for interacting with iGOT portal to fetch MDO admin/leader data"""
    
    IGOT_API_URL = "https://portal.igotkarmayogi.gov.in/api/private/user/v1/search"
    IGOT_AUTH_TOKEN = "bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI5a04xTW1TcGVuVTAyam8zVHg1U2p0amhTOFVXeGVSUiJ9.LWAgFust4e0wntxqY8_MQjf5WQ9RSD6Hg45jX_NoCXY"
    
    async def get_mdo_admins(self, department_id: str) -> List[Dict]:
        """
        Fetch MDO admins and leaders from iGOT portal for a specific department.
        
        Args:
            department_id: The department (rootOrgId) to filter MDO admins by
            
        Returns:
            List of MDO admin/leader user objects with firstName, lastName, id, rootOrgId, organisations
            
        Raises:
            HTTPException: If the API call fails
        """
        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": self.IGOT_AUTH_TOKEN
            }
            
            payload = {
                "request": {
                    "filters": {
                        "organisations.roles": ["MDO_LEADER", "MDO_ADMIN"],
                        "rootOrgId": department_id
                    },
                    "fields": ["firstName", "lastName", "id", "rootOrgId", "organisations"]
                }
            }
            
            logger.info(f"Fetching MDO admins for department: {department_id}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.IGOT_API_URL,
                    json=payload,
                    headers=headers
                )
                
                response.raise_for_status()
                data = response.json()
                
                # Extract users from response
                users = data.get("result", {}).get("response", {}).get("content", [])
                
                logger.info(f"Found {len(users)} MDO admins/leaders")
                
                return users
                
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
