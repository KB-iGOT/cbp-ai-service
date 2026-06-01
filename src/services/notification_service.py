from functools import lru_cache
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

IST = ZoneInfo("Asia/Kolkata")

from ..core.configs import settings
from ..core.logger import logger

TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "templates" / "emails"


@lru_cache(maxsize=None)
def _load_template(filename: str) -> str:
    """Load and cache email template from disk. Cached after first read."""
    template_path = TEMPLATE_DIR / filename
    return template_path.read_text(encoding="utf-8")


class NotificationService:
    """Handles sending email notifications via the notification API."""

    SEND_ENDPOINT = "/v2/notification/send"

    def __init__(self):
        self.base_url = settings.NOTIFICATION_BASE_URL
        self.timeout = 30.0

    async def _send(self, payload: dict) -> dict:
        url = f"{self.base_url}{self.SEND_ENDPOINT}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            return response.json()

    async def send_designation_approval_email(
        self,
        designation_name: str,
        requested_by: str,
        organization: str,
        request_id: str,
        user_id: str
    ) -> None:
        """Send email notification to SPV admins when a new designation approval request is created."""
        if not settings.ENABLE_EMAIL_NOTIFICATION:
            logger.info("Email notifications disabled – skipping designation approval email")
            return

        from .user_search_service import user_search_service

        # Fetch SPV admin emails from iGOT portal
        body = {
            "request": {
                "filters": {
                    "status": 1,
                    "organisations.roles": ["SPV_ADMIN"],
                },
                "fields": ["profileDetails"],
            }
        }
        try:
            users = await user_search_service.search_users(body)
        except Exception as exc:
            logger.error(f"Failed to fetch SPV admins for designation approval email: {exc}")
            return

        if not users:
            logger.warning("No SPV admins found – skipping designation approval email")
            return

        admin_emails = []
        for user in users:
            profile = user.get("profileDetails", {})
            personal = profile.get("personalDetails", {})
            email = personal.get("primaryEmail")
            if email:
                admin_emails.append(email)

        if not admin_emails:
            logger.warning("No SPV admin emails found – skipping designation approval email")
            return

        spv_portal_url = settings.SPV_PORTAL_URL+"/app/home/designation-approval"
        submitted_on = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
        template_data = _load_template("new_designation_request_email.html")

        params = {
            "designationName": designation_name,
            "requestedBy": requested_by,
            "organization": organization,
            "submittedOn": submitted_on,
            "requestId": request_id,
            "approvalLink": spv_portal_url,
        }

        payload = {
            "request": {
                "notifications": [
                    {
                        "type": "email",
                        "priority": 1,
                        "ids": admin_emails,
                        "bccIds": [],
                        "action": {
                            "type": "email",
                            "category": "email",
                            "createdBy": {
                                "id": user_id,
                                "type": "user",
                            },
                            "template": {
                                "data": template_data,
                                "id": "new-designation-request",
                                "params": params,
                                "type": "email",
                                "config": {
                                    "subject": "New Designation Request received for Approval",
                                    "sender": "",
                                },
                            },
                        },
                    }
                ]
            }
        }

        try:
            result = await self._send(payload)
            logger.info(f"Designation approval email sent for request {request_id}: {result}")
        except Exception as exc:
            logger.error(f"Failed to send designation approval email for request {request_id}: {exc}")

    async def send_cbp_approval_email(
        self,
        mdo_id: str,
        request_name: str,
        requested_by: str,
        request_id: str,
    ) -> None:
        """Send email notification to MDO Admin/Leader when a CBP plan is submitted for approval."""
        if not settings.ENABLE_EMAIL_NOTIFICATION:
            logger.info("Email notifications disabled – skipping CBP approval email")
            return

        from .user_search_service import user_search_service

        try:
            # Use user search service to fetch user details
            body = {
                "request": {
                    "filters": {
                        "status": 1,
                        "organisations.roles": ["MDO_LEADER", "MDO_ADMIN"],
                        "userId": mdo_id,
                    },
                    "fields": ["firstName", "lastName", "id", "rootOrgId", "organisations", "roles", "profileDetails"],
                }
            }
            users = await user_search_service.search_users(body)
            if not users:
                logger.warning(f"No MDO admin found for mdo_id={mdo_id} – skipping CBP approval email")
                return

            # Extract email from profileDetails.personalDetails.primaryEmail
            mdo_emails = []
            for user in users:
                profile = user.get("profileDetails", {})
                personal = profile.get("personalDetails", {})
                email = personal.get("primaryEmail")
                if email:
                    mdo_emails.append(email)

            if not mdo_emails:
                logger.warning(f"No email found for MDO admin mdo_id={mdo_id} – skipping CBP approval email")
                return

            mdo_portal_url = settings.MDO_PORTAL_URL+"/app/home/ai-cbp-requests/acbp-list/review-request/"+request_id+"?source=mdo"
            submitted_on = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
            template_data = _load_template("cbplan_request_email.html")

            params = {
                "requestName": request_name,
                "requestedBy": requested_by,
                "submittedOn": submitted_on,
                "requestId": request_id,
                "approvalLink": mdo_portal_url,
            }

            payload = {
                "request": {
                    "notifications": [
                        {
                            "type": "email",
                            "priority": 1,
                            "ids": mdo_emails,
                            "bccIds": [],
                            "action": {
                                "type": "email",
                                "category": "email",
                                "createdBy": {
                                    "id": mdo_id,
                                    "type": "user",
                                },
                                "template": {
                                    "data": template_data,
                                    "id": "cbp-plan-approval-request",
                                    "params": params,
                                    "type": "email",
                                    "config": {
                                        "subject": f"New Capacity Building Plan for {request_name} Submitted for Your Approval",
                                        "sender": "",
                                    },
                                },
                            },
                        }
                    ]
                }
            }

            result = await self._send(payload)
            logger.info(f"CBP approval email sent to MDO {mdo_id} for request {request_id}: {result}")
        except Exception as exc:
            logger.error(f"Failed to send CBP approval email for request {request_id}: {exc}")


notification_service = NotificationService()
