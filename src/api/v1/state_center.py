from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
import httpx

from ...schemas.state_center import OrgTypeEnum, StateCenterResponse

from ...core.configs import settings
from ...models.user import User

from ...api.dependencies import get_current_active_user
from ...core.logger import logger


router = APIRouter(tags=["State Centers"])
  
@router.get("/state-center/", response_model=List[StateCenterResponse])
async def get_all_state_centers(
    query: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
    sub_org_type: Optional[OrgTypeEnum] = OrgTypeEnum.ministry,
    current_user: User = Depends(get_current_active_user)
):
    """
    Get all state/centers from iGOT Karmayogi Portal
    
    Args:
        query: Search query for filtering organizations
        limit: Number of records to fetch (default: 200, max: 200)
        offset: Number of records to skip for pagination (default: 0)
        sbOrgType: Organization type filter (default: ministry)
        status_filter: Status filter (default: 1)
    """
    try:
        logger.info(f"Fetching state/centers - Query: {query}, Limit: {limit}, Offset: {offset}")
        
        if offset < 0:
            offset = 0
        
        api_url = f"{settings.KB_BASE_URL}/api/org/v1/search"
        
        request_body = {
            "request": {
                "filters": {
                    "status": 1,
                    "sbOrgType": sub_org_type
                },
                "sort_by": {
                    "createdDate": "desc"
                },
                "query": query if query else "",
                "limit": limit,
                "offset": offset,
                "fields": [
                    "identifier",
                    "orgName",
                    "description",
                    "parentOrgName",
                    "orgHierarchyFrameworkId",
                    "orgHierarchyFrameworkStatus",
                    "sbOrgType",
                    "sbOrgSubType"
                ]
            }
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(api_url, json=request_body, headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {settings.KB_AUTH_TOKEN}"
                })
            response.raise_for_status()
            
            data = response.json()
            
            # Extract the organizations from the API response
            if "result" in data:
                state_centers = data["result"].get("response", {}).get("content", [])
                total_count = data["result"].get("response", {}).get("count", 0)
            else:
                state_centers = data.get("data", [])
                total_count = len(state_centers)
            
            logger.info(f"Retrieved {len(state_centers)} state/centers from external API (Total: {total_count})")
            
            return state_centers
    except Exception as e:
        logger.error(f"Error fetching state/centers: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch state/centers"
        )
