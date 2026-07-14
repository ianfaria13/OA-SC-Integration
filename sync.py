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
  SC_CLIENT_ID        OAuth2 client ID from SchoolsBuddy
  SC_CLIENT_SECRET    OAuth2 client secret from SchoolsBuddy
  SC_SERVER           "emea", "apac", or "us"  (default: emea)
  SC_ORG_ID           SchoolsBuddy organisation ID (integer)
"""

import os
import sys
import time
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

OA_SUBDOMAIN     = os.environ["OA_SUBDOMAIN"]
OA_CLIENT_ID     = os.environ["OA_CLIENT_ID"]
OA_CLIENT_SECRET = os.environ["OA_CLIENT_SECRET"]
OA_REGION        = os.environ.get("OA_REGION", "can").lower()

SC_CLIENT_ID     = os.environ["SC_CLIENT_ID"]
SC_CLIENT_SECRET = os.environ["SC_CLIENT_SECRET"]
SC_SERVER        = os.environ.get("SC_SERVER", "emea").lower()
SC_ORG_ID        = int(os.environ["SC_ORG_ID"])

OA_BASE_URLS = {
    "can": "https://api.openapply.com",
    "cn":  "https://api.openapply.cn",
    "eu":  "https://api.openapply.com",
}
SC_BASE_URLS = {
    "emea": "https://publicapi-eu.schoolsbuddy.net",
    "apac": "https://publicapi-asia.schoolsbuddy.net",
    "us":   "https://publicapi-us.schoolsbuddy.net",
}
SC_AUTH_URLS = {
    "emea": "https://accounts1.schoolsbuddy.net/connect/token",
    "apac": "https://accounts2.schoolsbuddy.net/connect/token",
    "us":   "https://accounts3.schoolsbuddy.net/connect/token",
}

OA_BASE  = OA_BASE_URLS[OA_REGION]
SC_BASE  = SC_BASE_URLS[SC_SERVER]
SC_AUTH  = SC_AUTH_URLS[SC_SERVER]

# Statuses that should cause a student to be archived in SC
ARCHIVE_STATUSES = {"withdrawn", "graduated"}

# ── Gender mapping ────────────────────────────────────────────────────────────

GENDER_MAP = {
    "Male":        "Male",
    "Female":      "Female",
    "Non - Binary": "Unspecified",
}

# ── Rate-limit-aware request helper ──────────────────────────────────────────

def get_with_retry(url: str, headers: dict, params: dict = None, max_retries: int = 5) -> requests.Response:
    """GET with automatic retry on 429 (rate limit) and 500 (server error)."""
    for attempt in range(max_retries):
        resp = requests.get(url, headers=headers, params=params, timeout=60)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10)) + 2
            log.warning(f"Rate limited (429). Waiting {wait}s before retry {attempt + 1}/{max_retries}...")
            time.sleep(wait)
            continue
        if resp.status_code == 500:
            wait = 15 * (attempt + 1)
            log.warning(f"Server error (500). Waiting {wait}s before retry {attempt + 1}/{max_retries}...")
            log.warning(f"  Response: {resp.text[:200]}")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    raise Exception(f"Failed after {max_retries} retries: {url}")


def sc_get_with_retry(url: str, headers: dict, max_retries: int = 5) -> requests.Response:
    """GET for SC endpoints with retry on 429 and 500."""
    for attempt in range(max_retries):
        resp = requests.get(url, headers=headers, timeout=60)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 10)) + 2
            log.warning(f"SC rate limited (429). Waiting {wait}s before retry {attempt + 1}/{max_retries}...")
            time.sleep(wait)
            continue
        if resp.status_code == 500:
            wait = 15 * (attempt + 1)
            log.warning(f"SC server error (500). Waiting {wait}s before retry {attempt + 1}/{max_retries}...")
            log.warning(f"  Response: {resp.text[:200]}")
            time.sleep(wait)
            continue
        if not resp.ok:
            log.error(f"SC error: {resp.status_code} — {resp.text}")
        resp.raise_for_status()
        return resp
    raise Exception(f"SC failed after {max_retries} retries: {url}")

# ── OpenApply helpers ─────────────────────────────────────────────────────────

def oa_get_token() -> str:
    """Obtain a Bearer token from OpenApply using client credentials."""
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
    """Fetch all enrolled + withdrawn + graduated students from OA."""
    headers  = {"Authorization": f"Bearer {token}"}
    students = []

    for status in ("enrolled", "withdrawn", "graduated"):
        page = 1
        while True:
            resp = get_with_retry(
                f"{OA_BASE}/api/v3/students",
                headers=headers,
                params={
                    "status":   status,
                    "per_page": 200,
                    "page":     page,
                    "fields":   "id,first_name,last_name,preferred_name,nickname,"
                                "grade,gender,status,updated_at,parent_ids,birth_date",
                },
            )
            data  = resp.json()
            batch = data.get("students", [])
            students.extend(batch)
            if page >= data.get("meta", {}).get("pages", 1):
                break
            page += 1

    log.info(f"OA: fetched {len(students)} students total")
    return students


def oa_get_parents(token: str, parent_ids: set) -> dict[int, dict]:
    """Fetch parent records by ID. Returns a dict keyed by parent ID."""
    if not parent_ids:
        return {}
    headers = {"Authorization": f"Bearer {token}"}
    parents = {}
    page    = 1
    while True:
        resp = get_with_retry(
            f"{OA_BASE}/api/v3/parents",
            headers=headers,
            params={"per_page": 200, "page": page},
        )
        data = resp.json()
        for p in data.get("parents", []):
            if p["id"] in parent_ids:
                parents[p["id"]] = p
        if page >= data.get("meta", {}).get("pages", 1):
            break
        page += 1
    return parents

# ── SchoolsBuddy helpers ──────────────────────────────────────────────────────

def sc_get_token() -> str:
    """Obtain a Bearer token from SchoolsBuddy using client credentials."""
    resp = requests.post(SC_AUTH, data={
        "grant_type":    "client_credentials",
        "client_id":     SC_CLIENT_ID,
        "client_secret": SC_CLIENT_SECRET,
    }, timeout=30)
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise ValueError(f"SC auth response did not contain access_token: {resp.text}")
    log.info("SC: authentication successful")
    return token


def sc_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }


def sc_get_all_students(token: str) -> dict[str, dict]:
    """
    Fetch all students from SC (including archived).
    Returns a dict keyed by studentId (= OA id as string).
    """
    students = {}
    page     = 1
    while True:
        url = (f"{SC_BASE}/api/v1/Students"
               f"?OrganisationIds={SC_ORG_ID}&IncludeArchived=true"
               f"&PageSize=200&PageNumber={page}")
        resp = sc_get_with_retry(url, sc_headers(token))
        data = resp.json()
        for s in data.get("data", []):
            if s.get("studentId"):
                students[s["studentId"]] = s
        if page >= data.get("totalPages", 1):
            break
        page += 1
    log.info(f"SC: found {len(students)} existing students")
    return students


def sc_get_all_contacts(token: str) -> dict[str, dict]:
    """
    Fetch all contacts from SC.
    Returns a dict keyed by contactId (= OA parent id as string).
    """
    contacts = {}
    page     = 1
    while True:
        url = (f"{SC_BASE}/api/v1/Contacts"
               f"?OrganisationIds={SC_ORG_ID}&IncludeArchived=true"
               f"&PageSize=200&PageNumber={page}")
        resp = sc_get_with_retry(url, sc_headers(token))
        data = resp.json()
        for c in data.get("data", []):
            if c.get("contactId"):
                contacts[c["contactId"]] = c
        if page >= data.get("totalPages", 1):
            break
        page += 1
    log.info(f"SC: found {len(contacts)} existing contacts")
    return contacts


def sc_create_student(token: str, oa_student: dict) -> dict:
    payload = {
        "studentId":   str(oa_student["id"]),
        "firstName":   oa_student["first_name"],
        "lastName":    oa_student["last_name"],
        "otherName":   oa_student.get("preferred_name") or oa_student.get("nickname") or oa_student.get("other_name"),
        "grade":       oa_student.get("grade") or "Unknown",
        "gender":      GENDER_MAP.get(oa_student.get("gender", ""), "Unspecified"),
        "dateOfBirth": oa_student.get("birth_date"),
    }
    log.info(f"  SC payload: {payload}")
    resp = requests.post(
        f"{SC_BASE}/api/v1/Students",
        headers=sc_headers(token),
        params={"organisationId": SC_ORG_ID},
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        log.error(f"  SC error response: {resp.status_code} — {resp.text}")
    resp.raise_for_status()
    return resp.json()

def sc_update_student(token: str, sc_id: int, oa_student: dict) -> None:
    payload = {
        "firstName":   oa_student["first_name"],
        "lastName":    oa_student["last_name"],
        "otherName":   oa_student.get("preferred_name") or oa_student.get("nickname") or oa_student.get("other_name"),
        "grade":       oa_student.get("grade") or "Unknown",
        "gender":      GENDER_MAP.get(oa_student.get("gender") or "", "Unspecified"),
        "dateOfBirth": oa_student.get("birth_date"),
        "isArchived":  False,
    }
    resp = requests.put(
        f"{SC_BASE}/api/v1/Students/{sc_id}",
        headers=sc_headers(token),
        params={"organisationId": SC_ORG_ID},
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        log.error(f"  SC update error: {resp.status_code} — {resp.text}")
    resp.raise_for_status()



def sc_archive_student(token: str, sc_id: int, archive: bool = True) -> None:
    resp = requests.post(
        f"{SC_BASE}/api/v1/Students/BulkArchive",
        headers=sc_headers(token),
        json={"archive": archive, "ids": [sc_id]},
        timeout=30,
    )
    resp.raise_for_status()


def sc_create_contact(token: str, oa_parent: dict) -> dict:
    payload = {
        "contactId":         str(oa_parent["id"]),
        "firstName":         oa_parent.get("first_name", ""),
        "lastName":          oa_parent.get("last_name", ""),
        "emailAddress":      oa_parent.get("email", ""),
        "mobilePhoneNumber": oa_parent.get("mobile_phone"),
        "phoneNumber":       oa_parent.get("phone"),
    }
    resp = requests.post(
        f"{SC_BASE}/api/v1/Contacts",
        headers=sc_headers(token),
        params={"organisationId": SC_ORG_ID},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def sc_update_contact(token: str, sc_contact_id: int, oa_parent: dict) -> None:
    payload = {
        "firstName":         oa_parent.get("first_name", ""),
        "lastName":          oa_parent.get("last_name", ""),
        "emailAddress":      oa_parent.get("email", ""),
        "mobilePhoneNumber": oa_parent.get("mobile_phone"),
        "phoneNumber":       oa_parent.get("phone"),
    }
    resp = requests.put(
        f"{SC_BASE}/api/v1/Contacts/{sc_contact_id}",
        headers=sc_headers(token),
        params={"organisationId": SC_ORG_ID},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()


def sc_link_contacts(token: str, sc_student_id: int, contact_ids: list[int]) -> None:
    if not contact_ids:
        return
    resp = requests.post(
        f"{SC_BASE}/api/v1/Students/{sc_student_id}/contacts",
        headers=sc_headers(token),
        params={"organisationId": SC_ORG_ID},
        json=contact_ids,
        timeout=30,
    )
    resp.raise_for_status()


def sc_unlink_contacts(token: str, sc_student_id: int, contact_ids: list[int]) -> None:
    """Remove contacts from a student without deleting the contact itself."""
    if not contact_ids:
        return
    resp = requests.delete(
        f"{SC_BASE}/api/v1/Students/{sc_student_id}/contacts",
        headers=sc_headers(token),
        params={"organisationId": SC_ORG_ID},
        json=contact_ids,
        timeout=30,
    )
    if not resp.ok:
        log.error(f"  SC unlink contacts error: {resp.status_code} — {resp.text}")
    resp.raise_for_status()

# ── Comparison helpers ────────────────────────────────────────────────────────

def needs_student_update(oa: dict, sc: dict) -> bool:
    # Normalise birth_date: OA returns "YYYY-MM-DD", SC returns "YYYY-MM-DDT00:00:00"
    oa_dob = oa.get("birth_date")
    sc_dob = sc.get("dateOfBirth")
    if sc_dob and "T" in sc_dob:
        sc_dob = sc_dob.split("T")[0]

    oa_gender = GENDER_MAP.get(oa.get("gender") or "", "Unspecified")
    oa_other  = oa.get("preferred_name") or oa.get("nickname") or oa.get("other_name")

    return (
        oa["first_name"]       != sc.get("firstName")
        or oa["last_name"]     != sc.get("lastName")
        or oa_other            != sc.get("otherName")
        or oa.get("grade", "") != sc.get("grade")
        or oa_gender           != sc.get("gender")
        or oa_dob              != sc_dob
    )


def needs_contact_update(oa: dict, sc: dict) -> bool:
    return (
        oa.get("first_name", "") != sc.get("firstName")
        or oa.get("last_name", "") != sc.get("lastName")
        or oa.get("email", "") != sc.get("emailAddress")
        or oa.get("mobile_phone") != sc.get("mobilePhoneNumber")
        or oa.get("phone") != sc.get("phoneNumber")
    )


def needs_staff_update(oa: dict, sc: dict) -> bool:
    oa_phone = (oa.get("properties") or {}).get("phone")
    return (
        oa.get("first_name", "") != sc.get("firstName")
        or oa.get("last_name", "") != sc.get("lastName")
        or oa.get("email", "")     != sc.get("email")
        or oa_phone                != sc.get("phoneNumber")
    )


# ── OA Staff helpers ──────────────────────────────────────────────────────────

def oa_get_all_staff(token: str) -> list[dict]:
    """Fetch all staff from OA (active + deleted)."""
    headers = {"Authorization": f"Bearer {token}"}
    staff   = []
    page    = 1
    while True:
        resp = get_with_retry(
            f"{OA_BASE}/api/v3/staff",
            headers=headers,
            params={"per_page": 200, "page": page},
        )
        data  = resp.json()
        batch = data.get("staff", [])
        staff.extend(batch)
        if page >= data.get("meta", {}).get("pages", 1):
            break
        page += 1
    log.info(f"OA: fetched {len(staff)} staff total")
    return staff


# ── SC Staff helpers ──────────────────────────────────────────────────────────

def sc_get_all_staff(token: str) -> dict[str, dict]:
    """Fetch all staff from SC. Returns dict keyed by staffId (= OA id as string)."""
    staff = {}
    page  = 1
    while True:
        url  = (f"{SC_BASE}/api/v1/Staff"
                f"?OrganisationIds={SC_ORG_ID}&IncludeArchived=true"
                f"&PageSize=200&PageNumber={page}")
        resp = sc_get_with_retry(url, sc_headers(token))
        data = resp.json()
        for s in data.get("data", []):
            if s.get("staffId"):
                staff[s["staffId"]] = s
        if page >= data.get("totalPages", 1):
            break
        page += 1
    log.info(f"SC: found {len(staff)} existing staff")
    return staff


def sc_create_staff(token: str, oa_staff: dict) -> dict:
    oa_phone = (oa_staff.get("properties") or {}).get("phone")
    payload  = {
        "staffId":     str(oa_staff["id"]),
        "firstName":   oa_staff.get("first_name", ""),
        "lastName":    oa_staff.get("last_name", ""),
        "email":       oa_staff.get("email", ""),
        "phoneNumber": oa_phone,
        "role":        "Staff",
        "isArchived":  False,
    }
    resp = requests.post(
        f"{SC_BASE}/api/v1/Staff",
        headers=sc_headers(token),
        params={"organisationId": SC_ORG_ID},
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        log.error(f"  SC create staff error: {resp.status_code} — {resp.text}")
    resp.raise_for_status()
    return resp.json()


def sc_update_staff(token: str, sc_id: int, oa_staff: dict) -> None:
    oa_phone = (oa_staff.get("properties") or {}).get("phone")
    payload  = {
        "firstName":   oa_staff.get("first_name", ""),
        "lastName":    oa_staff.get("last_name", ""),
        "email":       oa_staff.get("email", ""),
        "phoneNumber": oa_phone,
    }
    resp = requests.put(
        f"{SC_BASE}/api/v1/Staff/{sc_id}",
        headers=sc_headers(token),
        params={"organisationId": SC_ORG_ID},
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        log.error(f"  SC update staff error: {resp.status_code} — {resp.text}")
    resp.raise_for_status()


def sc_archive_staff(token: str, sc_id: int, archive: bool = True) -> None:
    resp = requests.post(
        f"{SC_BASE}/api/v1/Staff/BulkArchive",
        headers=sc_headers(token),
        params={"organisationId": SC_ORG_ID},
        json={"archive": archive, "ids": [sc_id]},
        timeout=30,
    )
    resp.raise_for_status()


# ── Main sync logic ───────────────────────────────────────────────────────────

def run_sync():
    log.info("=== OA → SC Sync starting ===")
    stats = {"created": 0, "updated": 0, "archived": 0,
             "contacts_created": 0, "contacts_updated": 0,
             "staff_created": 0, "staff_updated": 0,
             "errors": 0}

    # ── 1. Authenticate with both systems ────────────────────────────────────
    log.info("Authenticating with OpenApply...")
    oa_token = oa_get_token()

    log.info("Authenticating with SchoolsBuddy...")
    sc_token = sc_get_token()

    # ── 2. Pull all students from OA ─────────────────────────────────────────
    oa_students = oa_get_all_students(oa_token)

    all_parent_ids = set()
    for s in oa_students:
        all_parent_ids.update(s.get("parent_ids", []))

    # ── 3. Pull all parents from OA ──────────────────────────────────────────
    log.info(f"Fetching {len(all_parent_ids)} unique parents from OA...")
    oa_parents = oa_get_parents(oa_token, all_parent_ids)

    # ── 4. Pull existing SC data ──────────────────────────────────────────────
    log.info("Fetching existing students from SC...")
    sc_students = sc_get_all_students(sc_token)

    log.info("Fetching existing contacts from SC...")
    sc_contacts = sc_get_all_contacts(sc_token)

    # ── 5. Process each OA student ───────────────────────────────────────────
    for oa_s in oa_students:
        oa_id_str = str(oa_s["id"])
        status    = oa_s.get("status", "")

        try:
            existing_sc = sc_students.get(oa_id_str)

            # ── ARCHIVE: withdrawn or graduated ──────────────────────────────
            if status in ARCHIVE_STATUSES:
                if existing_sc and not existing_sc.get("isArchived"):
                    log.info(f"Archiving student OA:{oa_id_str} ({oa_s['last_name']}, {oa_s['first_name']}) — status: {status}")
                    sc_archive_student(sc_token, existing_sc["id"], archive=True)
                    stats["archived"] += 1
                continue

            # ── CREATE: enrolled but not yet in SC ───────────────────────────
            if not existing_sc:
                log.info(f"Creating student OA:{oa_id_str} ({oa_s['last_name']}, {oa_s['first_name']})")
                created       = sc_create_student(sc_token, oa_s)
                sc_student_id = created["id"]
                stats["created"] += 1

            # ── UPDATE: enrolled, already in SC ──────────────────────────────
            else:
                sc_student_id = existing_sc["id"]

                # Un-archive if they were previously archived
                if existing_sc.get("isArchived"):
                    log.info(f"Re-activating previously archived student OA:{oa_id_str}")
                    sc_archive_student(sc_token, sc_student_id, archive=False)

                if needs_student_update(oa_s, existing_sc):
                    log.info(f"Updating student OA:{oa_id_str} ({oa_s['last_name']}, {oa_s['first_name']})")
                    sc_update_student(sc_token, sc_student_id, oa_s)
                    stats["updated"] += 1

            # ── PARENTS ───────────────────────────────────────────────────────
            # Build the set of SC contact IDs that SHOULD be linked to this student
            expected_sc_contact_ids = set()
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
                    created_c            = sc_create_contact(sc_token, oa_p)
                    sc_contacts[pid_str] = created_c
                    new_contact_ids.append(created_c["id"])
                    expected_sc_contact_ids.add(created_c["id"])
                    stats["contacts_created"] += 1
                else:
                    if needs_contact_update(oa_p, existing_c):
                        log.info(f"  Updating contact for parent OA:{pid_str}")
                        sc_update_contact(sc_token, existing_c["id"], oa_p)
                        stats["contacts_updated"] += 1
                    new_contact_ids.append(existing_c["id"])
                    expected_sc_contact_ids.add(existing_c["id"])

            # Link any newly created contacts to the student
            if new_contact_ids:
                sc_link_contacts(sc_token, sc_student_id, new_contact_ids)

            # Remove contacts that are currently linked but shouldn't be
            current_sc_contact_ids = set(existing_sc.get("relatedPeople", []) if existing_sc else [])
            contacts_to_remove = current_sc_contact_ids - expected_sc_contact_ids
            if contacts_to_remove:
                log.info(f"  Removing {len(contacts_to_remove)} incorrect contact link(s) from student OA:{oa_id_str}")
                sc_unlink_contacts(sc_token, sc_student_id, list(contacts_to_remove))

        except Exception as e:
            log.error(f"Error processing student OA:{oa_id_str}: {e}")
            stats["errors"] += 1
            continue

    # ── 6. Staff sync ─────────────────────────────────────────────────────────
    log.info("Fetching staff from OA...")
    oa_staff_list = oa_get_all_staff(oa_token)

    log.info("Fetching existing staff from SC...")
    sc_staff = sc_get_all_staff(sc_token)

    # ── Step B: Create or update active OA staff in SC ───────────────────────
    for oa_s in oa_staff_list:
        oa_id_str = str(oa_s["id"])

        try:
            existing_sc = sc_staff.get(oa_id_str)

            # ── CREATE: not yet in SC ─────────────────────────────────────────
            if not existing_sc:
                log.info(f"Creating staff OA:{oa_id_str} ({oa_s.get('last_name')}, {oa_s.get('first_name')})")
                sc_create_staff(sc_token, oa_s)
                stats["staff_created"] += 1

            # ── UPDATE: already in SC ─────────────────────────────────────────
            else:
                if existing_sc.get("isArchived"):
                    log.info(f"Re-activating staff OA:{oa_id_str}")
                    sc_archive_staff(sc_token, existing_sc["id"], archive=False)

                if needs_staff_update(oa_s, existing_sc):
                    log.info(f"Updating staff OA:{oa_id_str} ({oa_s.get('last_name')}, {oa_s.get('first_name')})")
                    sc_update_staff(sc_token, existing_sc["id"], oa_s)
                    stats["staff_updated"] += 1

        except Exception as e:
            log.error(f"Error processing staff OA:{oa_id_str}: {e}")
            stats["errors"] += 1
            continue

    # ── 7. Summary ────────────────────────────────────────────────────────────
    log.info("=== Sync complete ===")
    log.info(f"Students  — created: {stats['created']}, updated: {stats['updated']}, archived: {stats['archived']}")
    log.info(f"Contacts  — created: {stats['contacts_created']}, updated: {stats['contacts_updated']}")
    log.info(f"Staff     — created: {stats['staff_created']}, updated: {stats['staff_updated']}")
    if stats["errors"]:
        log.warning(f"Errors: {stats['errors']} (check logs above)")
    else:
        log.info("No errors.")


if __name__ == "__main__":
    run_sync()
