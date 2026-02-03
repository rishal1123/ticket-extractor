from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Ticket:
    portal: str
    ticket_id: str
    address: Optional[str] = None
    customer_name: Optional[str] = None
    ticket_type: Optional[str] = None
    portal_created_at: Optional[datetime] = None  # When ticket was created on ISP portal
    service_type: Optional[str] = None
    status: Optional[str] = None
    kpi: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None         # When we first extracted it to our DB
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None       # When ticket disappeared from portal
    id: Optional[int] = None
    in_znuny: bool = False
    znuny_ticket_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "portal": self.portal,
            "ticket_id": self.ticket_id,
            "address": self.address,
            "customer_name": self.customer_name,
            "ticket_type": self.ticket_type,
            "portal_created_at": self.portal_created_at.isoformat() if self.portal_created_at else None,
            "service_type": self.service_type,
            "status": self.status,
            "kpi": self.kpi,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "in_znuny": self.in_znuny,
            "znuny_ticket_id": self.znuny_ticket_id
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Ticket":
        portal_created_at = data.get("portal_created_at")
        if isinstance(portal_created_at, str):
            portal_created_at = datetime.fromisoformat(portal_created_at)

        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)

        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)

        completed_at = data.get("completed_at")
        if isinstance(completed_at, str):
            completed_at = datetime.fromisoformat(completed_at)

        return cls(
            id=data.get("id"),
            portal=data["portal"],
            ticket_id=data["ticket_id"],
            address=data.get("address"),
            customer_name=data.get("customer_name"),
            ticket_type=data.get("ticket_type"),
            portal_created_at=portal_created_at,
            service_type=data.get("service_type"),
            status=data.get("status"),
            kpi=data.get("kpi"),
            notes=data.get("notes"),
            created_at=created_at,
            updated_at=updated_at,
            completed_at=completed_at,
            in_znuny=data.get("in_znuny", False),
            znuny_ticket_id=data.get("znuny_ticket_id")
        )
