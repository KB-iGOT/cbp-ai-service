from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
import httpx

from ...schemas.state_center import OrgTypeEnum

from ...schemas.department import DepartmentResponse
from ...core.configs import settings
from ...models.user import User
from ...api.dependencies import get_current_active_user
from ...core.logger import logger

router = APIRouter(tags=["Departments"])

# Department APIs

@router.get("/department/state-center/{state_center_id}", response_model=List[DepartmentResponse])
async def get_departments_by_state_center(
    state_center_id: str,  # Changed from uuid.UUID to str
    limit: int = 9999,
    offset: int = 0,
    sub_org_type: Optional[OrgTypeEnum] = OrgTypeEnum.state,
    current_user: User = Depends(get_current_active_user)
):
    """
    Get all departments for a specific state/center from iGOT Karmayogi Portal
    
    Args:
        state_center_id: Ministry or State identifier
        limit: Number of records to fetch (default: 9999)
        offset: Number of records to skip for pagination (default: 0)
    """
    try:
        logger.info(f"Fetching departments for state/center ID: {state_center_id}")
        
        api_url = f"{settings.KB_BASE_URL}/api/org/v1/search"
        
        request_body = {
            "request": {
                "filters": {
                    "status": 1,
                    "ministryOrStateType": sub_org_type,
                    "ministryOrStateId": state_center_id
                },
                "sort_by": {
                    "createdDate": "desc"
                },
                "limit": limit,
                "offset": offset,
                "fields": [
                    "identifier",
                    "orgName",
                    "description",
                    "parentOrgName",
                    "ministryOrStateId",
                    "ministryOrStateType",
                    "ministryOrStateName",
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
            
            # Extract departments from the API response
            if "result" in data:
                departments = data["result"].get("response", {}).get("content", [])
            else:
                departments = data.get("data", [])
            
            # if not departments:
            #     logger.warning(f"No departments found for state/center ID: {state_center_id}")
            #     raise HTTPException(
            #         status_code=status.HTTP_404_NOT_FOUND,
            #         detail="No departments found for this state/center"
            #     )
            
            logger.info(f"Retrieved {len(departments)} departments for state/center ID: {state_center_id}")
            
            # Parse and validate with Pydantic
            validated_departments = [DepartmentResponse(**dept) for dept in departments]
            
            return validated_departments
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching departments by state/center: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch departments"
        )