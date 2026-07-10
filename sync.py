"""
OpenApply → SchoolsBuddy Daily Sync
------------------------------------
Syncs enrolled students and their parents from OA into SC.
- New enrolled students are created in SC
- Data changes (name, grade, contacts) are updated in SC
- Withdrawn / graduated students are archived in SC
  (parents are auto-archived by SC when they have no remaining children)

Required environment variables:
  OA_SUBDOMAIN        e.g. "myschool"
  OA_CLIENT_ID        OAuth2 client ID from OA
  OA_CLIENT_SECRET    OAuth2 client secret from OA
  OA_REGION           "can", "cn", or "eu"  (default: can)
  SC_API_KEY          Bearer token for SchoolsBuddy Public API
  SC_SERVER           "emea", "apac", or "us"  (default: emea)
  SC_ORG_ID           SchoolsBuddy organisation ID (integer)
"""

import os
import sys
import logging
import requests
from datetime import datetime, timezone

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Config from environment ───────────────────────────────────────────────────

OA_SUBDOMAIN    = os.environ["OA_SUBDOMAIN"]
OA_CLIENT_ID    = os.environ["OA_CLIENT_ID"]
OA_CLIENT_SECRET = os.environ["OA_CLIENT_SECRET"]
OA_REGION       = os.environ.get("OA_REGION", "can").lower()

SC_API_KEY      = os.environ["SC_API_KEY"]
SC_SERVER       = os.environ.get("SC_SERVER", "emea").lower()
SC_ORG_ID       = int(os.environ["SC_ORG_ID"])

OA_BASE_URLS = {
    "can": "https://api.openapply.com",
    "cn":  "https://api.openapply.cn",
    "eu":  "https://api.openapply.com",   # EU schools still use the CAN API endpoint
}
SC_BASE_URLS = {
    "emea": "https://publicapi-eu.schoolsbuddy.net",
    "apac": "https://publicapi-asia.schoolsbuddy.net",
    "us":   "https://publicapi-us.schoolsbuddy.net",
}

OA_BASE = OA_BASE_URLS[OA_REGION]
SC_BASE = SC_BASE_URLS[SC_SERVER]

# Statuses that should cause a student to be archived in SC
ARCHIVE_STATUSES = {"withdrawn", "graduated"}

# ── Gender mapping ────────────────────────────────────────────────────────────

GENDER_MAP = {
    "Male":      "Male",
    "Female":    "Female",
    "Non - Binary": "Unspecified",
}

# ── OpenApply helpers ─────────────────────────────────────────────────────────

def oa_get_token() -> str:
    """Obtain a Bearer token from OpenApply using client credentials."""
    # OA uses subdomain-specific auth URL
    auth_url = f"https://{OA_SUBDOMAIN}.openapply.com/oauth/token"
    if OA_REGION == "cn":
        auth_url = f"https://{OA_SUBDOMAIN}.openapply.cn/oauth/token"

    resp = requests.post(auth_url, data={
        "grant_type":    "client_credentials",
        "client_id":     OA_CLIENT_ID,
        "client_secret": OA_CLIENT_SECRET,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def oa_get_all_students(token: str) -> list[dict]:
    """
    Fetch all enrolled + withdrawn + graduated students from OA.
    We fetch enrolled separately to create/update, and withdrawn/graduated
    to archive. Uses since_date=yesterday would miss historical data on first
    run, so we always fetch the full list and let SC-side checks do the diff.
    """
    headers = {"Authorization": f"Bearer {token}"}
    students = []

    for status in ("enrolled", "withdrawn", "graduated"):
        page = 1
        while True:
            resp = requests.get(
                f"{OA_BASE}/api/v3/students",
                headers=headers,
                params={
                    "status":   status,
                    "per_page": 200,
                    "page":     page,
                    "fields":   "id,first_name,last_name,preferred_name,other_name,"
                                "grade,gender,status,updated_at,parent_ids",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("students", [])
            students.extend(batch)
            if page >= data.get("meta", {}).get("pages", 1):
                break
            page += 1

    log.info(f"OA: fetched {len(students)} students total")
    return students


def oa_get_parents(token: str, parent_ids: list[int]) -> dict[int, dict]:
    """Fetch parent records by ID. Returns a dict keyed by parent ID."""
    if not parent_ids:
        return {}
    headers = {"Authorization": f"Bearer {token}"}
    parents = {}
    # OA doesn't support batch parent fetch by IDs directly, so we pull page by page
    # and filter; for large schools this is still manageable
    page = 1
    while True:
        resp = requests.get(
            f"{OA_BASE}/api/v3/parents",
            headers=headers,
            params={"per_page": 200, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for p in data.get("parents", []):
            if p["id"] in parent_ids:
                parents[p["id"]] = p
        if page >= data.get("meta", {}).get("pages", 1):
            break
        page += 1
    return parents

# ── SchoolsBuddy helpers ──────────────────────────────────────────────────────

SC_HEADERS = {
    "Authorization": f"Bearer {SC_API_KEY}",
    "Content-Type":  "application/json",
}


def sc_get_all_students() -> dict[str, dict]:
    """
    Fetch all students from SC (including archived).
    Returns a dict keyed by studentId (which we set to the OA id as a string).
    """
    students = {}
    page = 1
    while True:
        resp = requests.get(
            f"{SC_BASE}/api/v1/Students",
            headers=SC_HEADERS,
            params={
                "OrganisationIds": SC_ORG_ID,
                "IncludeArchived": True,
                "PageSize":        200,
                "PageNumber":      page,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for s in data.get("data", []):
            if s.get("studentId"):
                students[s["studentId"]] = s
        if page >= data.get("totalPages", 1):
            break
        page += 1
    log.info(f"SC: found {len(students)} existing students")
    return students


def sc_get_all_contacts() -> dict[str, dict]:
    """
    Fetch all contacts from SC.
    Returns a dict keyed by contactId (which we set to the OA parent id as a string).
    """
    contacts = {}
    page = 1
    while True:
        resp = requests.get(
            f"{SC_BASE}/api/v1/Contacts",
            headers=SC_HEADERS,
            params={
                "OrganisationIds": SC_ORG_ID,
                "IncludeArchived": True,
                "PageSize":        200,
                "PageNumber":      page,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for c in data.get("data", []):
            if c.get("contactId"):
                contacts[c["contactId"]] = c
        if page >= data.get("totalPages", 1):
            break
        page += 1
    log.info(f"SC: found {len(contacts)} existing contacts")
    return contacts


def sc_create_student(oa_student: dict) -> dict:
    payload = {
        "studentId":    str(oa_student["id"]),
        "firstName":    oa_student["first_name"],
        "lastName":     oa_student["last_name"],
        "otherName":    oa_student.get("preferred_name") or oa_student.get("other_name"),
        "grade":        oa_student.get("grade", ""),
        "gender":       GENDER_MAP.get(oa_student.get("gender", ""), "Unspecified"),
    }
    resp = requests.post(
        f"{SC_BASE}/api/v1/Students",
        headers=SC_HEADERS,
        params={"organisationId": SC_ORG_ID},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def sc_update_student(sc_id: int, oa_student: dict) -> None:
    payload = {
        "firstName":    oa_student["first_name"],
        "lastName":     oa_student["last_name"],
        "otherName":    oa_student.get("preferred_name") or oa_student.get("other_name"),
        "grade":        oa_student.get("grade", ""),
        "gender":       GENDER_MAP.get(oa_student.get("gender", ""), "Unspecified"),
    }
    resp = requests.put(
        f"{SC_BASE}/api/v1/Students/{sc_id}",
        headers=SC_HEADERS,
        params={"organisationId": SC_ORG_ID},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()


def sc_archive_student(sc_id: int) -> None:
    resp = requests.post(
        f"{SC_BASE}/api/v1/Students/BulkArchive",
        headers=SC_HEADERS,
        json={"archive": True, "ids": [sc_id]},
        timeout=30,
    )
    resp.raise_for_status()


def sc_create_contact(oa_parent: dict) -> dict:
    payload = {
        "contactId":    str(oa_parent["id"]),
        "firstName":    oa_parent.get("first_name", ""),
        "lastName":     oa_parent.get("last_name", ""),
        "emailAddress": oa_parent.get("email", ""),
        "mobilePhoneNumber": oa_parent.get("mobile_phone"),
        "phoneNumber":  oa_parent.get("phone"),
    }
    resp = requests.post(
        f"{SC_BASE}/api/v1/Contacts",
        headers=SC_HEADERS,
        params={"organisationId": SC_ORG_ID},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def sc_update_contact(sc_contact_id: int, oa_parent: dict) -> None:
    payload = {
        "firstName":    oa_parent.get("first_name", ""),
        "lastName":     oa_parent.get("last_name", ""),
        "emailAddress": oa_parent.get("email", ""),
        "mobilePhoneNumber": oa_parent.get("mobile_phone"),
        "phoneNumber":  oa_parent.get("phone"),
    }
    resp = requests.put(
        f"{SC_BASE}/api/v1/Contacts/{sc_contact_id}",
        headers=SC_HEADERS,
        params={"organisationId": SC_ORG_ID},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()


def sc_link_contacts(sc_student_id: int, contact_ids: list[int]) -> None:
    if not contact_ids:
        return
    resp = requests.post(
        f"{SC_BASE}/api/v1/Students/{sc_student_id}/contacts",
        headers=SC_HEADERS,
        params={"organisationId": SC_ORG_ID},
        json=contact_ids,
        timeout=30,
    )
    resp.raise_for_status()

# ── Main sync logic ───────────────────────────────────────────────────────────

def needs_student_update(oa: dict, sc: dict) -> bool:
    """Return True if any tracked field differs between OA and SC."""
    return (
        oa["first_name"]                                    != sc.get("firstName")
        or oa["last_name"]                                  != sc.get("lastName")
        or (oa.get("preferred_name") or oa.get("other_name")) != sc.get("otherName")
        or oa.get("grade", "")                              != sc.get("grade")
        or GENDER_MAP.get(oa.get("gender", ""), "Unspecified") != sc.get("gender")
    )


def needs_contact_update(oa: dict, sc: dict) -> bool:
    return (
        oa.get("first_name", "")   != sc.get("firstName")
        or oa.get("last_name", "") != sc.get("lastName")
        or oa.get("email", "")     != sc.get("emailAddress")
        or oa.get("mobile_phone")  != sc.get("mobilePhoneNumber")
        or oa.get("phone")         != sc.get("phoneNumber")
    )


def run_sync():
    log.info("=== OA → SC Sync starting ===")
    stats = {"created": 0, "updated": 0, "archived": 0,
             "contacts_created": 0, "contacts_updated": 0, "errors": 0}

    # ── 1. Authenticate with OA ──────────────────────────────────────────────
    log.info("Authenticating with OpenApply...")
    oa_token = oa_get_token()

    # ── 2. Pull all relevant students from OA ────────────────────────────────
    oa_students = oa_get_all_students(oa_token)

    # Collect all parent IDs we'll need
    all_parent_ids = set()
    for s in oa_students:
        all_parent_ids.update(s.get("parent_ids", []))

    # ── 3. Pull all parents from OA ─────────────────────────────────────────
    log.info(f"Fetching {len(all_parent_ids)} unique parents from OA...")
    oa_parents = oa_get_parents(oa_token, all_parent_ids)

    # ── 4. Pull existing SC data ──────────────────────────────────────────────
    log.info("Fetching existing students from SC...")
    sc_students = sc_get_all_students()   # keyed by studentId (= OA id as str)

    log.info("Fetching existing contacts from SC...")
    sc_contacts = sc_get_all_contacts()   # keyed by contactId (= OA parent id as str)

    # ── 5. Process each OA student ───────────────────────────────────────────
    for oa_s in oa_students:
        oa_id_str = str(oa_s["id"])
        status    = oa_s.get("status", "")

        try:
            existing_sc = sc_students.get(oa_id_str)

            # ── ARCHIVE: withdrawn or graduated ─────────────────────────────
            if status in ARCHIVE_STATUSES:
                if existing_sc and not existing_sc.get("isArchived"):
                    log.info(f"Archiving student OA:{oa_id_str} ({oa_s['last_name']}, {oa_s['first_name']}) — status: {status}")
                    sc_archive_student(existing_sc["id"])
                    stats["archived"] += 1
                continue  # No further processing for non-enrolled students

            # ── CREATE: enrolled but not yet in SC ───────────────────────────
            if not existing_sc:
                log.info(f"Creating student OA:{oa_id_str} ({oa_s['last_name']}, {oa_s['first_name']})")
                created = sc_create_student(oa_s)
                sc_student_id = created["id"]
                stats["created"] += 1

            # ── UPDATE: enrolled, already in SC ──────────────────────────────
            else:
                sc_student_id = existing_sc["id"]

                # Un-archive if they were previously archived
                if existing_sc.get("isArchived"):
                    log.info(f"Re-activating previously archived student OA:{oa_id_str}")
                    # SC doesn't have a direct "unarchive" PUT; BulkArchive with archive=false
                    requests.post(
                        f"{SC_BASE}/api/v1/Students/BulkArchive",
                        headers=SC_HEADERS,
                        json={"archive": False, "ids": [sc_student_id]},
                        timeout=30,
                    ).raise_for_status()

                if needs_student_update(oa_s, existing_sc):
                    log.info(f"Updating student OA:{oa_id_str} ({oa_s['last_name']}, {oa_s['first_name']})")
                    sc_update_student(sc_student_id, oa_s)
                    stats["updated"] += 1

            # ── PARENTS: ensure each linked parent exists and is up to date ──
            new_contact_ids = []
            for pid in oa_s.get("parent_ids", []):
                oa_p = oa_parents.get(pid)
                if not oa_p:
                    log.warning(f"Parent OA:{pid} not found in fetched data, skipping")
                    continue

                pid_str    = str(pid)
                existing_c = sc_contacts.get(pid_str)

                if not existing_c:
                    log.info(f"  Creating contact for parent OA:{pid_str} ({oa_p.get('last_name')}, {oa_p.get('first_name')})")
                    created_c = sc_create_contact(oa_p)
                    sc_contacts[pid_str] = created_c   # cache for this run
                    new_contact_ids.append(created_c["id"])
                    stats["contacts_created"] += 1
                else:
                    if needs_contact_update(oa_p, existing_c):
                        log.info(f"  Updating contact for parent OA:{pid_str}")
                        sc_update_contact(existing_c["id"], oa_p)
                        stats["contacts_updated"] += 1
                    new_contact_ids.append(existing_c["id"])

            # Link any newly created contacts to the student
            if new_contact_ids:
                sc_link_contacts(sc_student_id, new_contact_ids)

        except Exception as e:
            log.error(f"Error processing student OA:{oa_id_str}: {e}")
            stats["errors"] += 1
            continue

    # ── 6. Summary ────────────────────────────────────────────────────────────
    log.info("=== Sync complete ===")
    log.info(f"Students  — created: {stats['created']}, updated: {stats['updated']}, archived: {stats['archived']}")
    log.info(f"Contacts  — created: {stats['contacts_created']}, updated: {stats['contacts_updated']}")
    if stats["errors"]:
        log.warning(f"Errors: {stats['errors']} (check logs above)")
    else:
        log.info("No errors.")


if __name__ == "__main__":
    run_sync()
