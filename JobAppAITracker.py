#!/usr/bin/env python3
"""
Job Application Tracker
Gmail → Gemini (Grok fallback) → jobs.json + terminal output

Usage:
  python JobAppAITracker.py                               # full run
  python JobAppAITracker.py --refresh                     # re-fetch emails, ignore cache
  python JobAppAITracker.py --refresh --start-date 2026-05-08
  python JobAppAITracker.py --status                      # print stats from saved jobs.json

Setup (data/ folder next to this file):
  data/config.json       → { "gemini_api_key": "...", "grok_api_key": "..." }
  data/credentials.json  → Gmail OAuth creds (download from Google Cloud Console)
"""

import sys, os, json, re, base64, time, argparse, datetime as dt, html
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, parse_qs
import threading, webbrowser

import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE        = Path(__file__).parent
DATA        = BASE / "data"
CREDENTIALS = DATA / "credentials.json"
TOKEN       = DATA / "gmail-token.json"
CONFIG      = DATA / "config.json"
EMAIL_CACHE = DATA / "emails-cache.json"
EMAIL_AUDIT_CACHE = DATA / "emails-audit-cache.json"
CLASS_CACHE = DATA / "classifications-cache.json"
JOBS_OUT    = DATA / "jobs.json"

DATA.mkdir(exist_ok=True)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SCAN_START_DATE = dt.date(2026, 5, 8)
TERM_START_DATE = dt.date(2026, 9, 1)
MAX_CANDIDATE_EMAILS = 2000
CACHE_VERSION = 12
CLASS_CACHE_VERSION = 4
DEFAULT_GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
DEFAULT_GROK_MODEL = "grok-3-mini"
DEFAULT_GROQ_CLOUD_MODEL = "llama-3.1-8b-instant"
DEFAULT_OPENROUTER_MODEL = "google/gemini-2.0-flash-001"
STATUSES = [
    "applied",
    "assessment_requested",
    "recruiter_contact",
    "interviewing",
    "final_round",
    "offer",
    "rejected",
    "needs_review",
]
ACTIVE_STATUSES = {"assessment_requested", "recruiter_contact", "interviewing", "final_round"}
STATUS_THRESHOLDS = {
    "applied": 0.55,
    "assessment_requested": 0.75,
    "recruiter_contact": 0.75,
    "interviewing": 0.75,
    "final_round": 0.78,
    "offer": 0.90,
    "rejected": 0.90,
}

# ── Config ────────────────────────────────────────────────────────────────────
def load_config():
    if not CONFIG.exists():
        sys.exit(f"""
❌  data/config.json not found. Create it:
{{
  "gemini_api_key": "YOUR_KEY",   ← https://aistudio.google.com/app/apikey
  "grok_api_key":   "YOUR_KEY",    ← https://console.x.ai/
  "openrouter_api_key": "...",     ← optional, https://openrouter.ai/keys
  "openrouter_model": "google/gemini-2.0-flash-001"
}}
""")
    return json.loads(CONFIG.read_text())

def config_value(config, *names):
    for name in names:
        value = config.get(name)
        if value:
            return value
    return None

def config_list(config, name, default):
    value = config.get(name)
    if not value:
        return list(default)
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return list(default)

# ── Gmail Auth ────────────────────────────────────────────────────────────────
def gmail_auth():
    if not CREDENTIALS.exists():
        sys.exit("""
❌  data/credentials.json not found.

Steps:
  1. https://console.cloud.google.com/ → new project → enable Gmail API
  2. APIs & Services → Credentials → Create OAuth2 Client ID (Desktop App)
  3. Download JSON → save as data/credentials.json
""")

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("🔄 Refreshing Gmail token...")
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
            creds = flow.run_local_server(port=3333, open_browser=True)

        TOKEN.write_text(creds.to_json())
        print("✅ Gmail authorised\n")

    return build("gmail", "v1", credentials=creds)

# ── Email Fetching ────────────────────────────────────────────────────────────
APPLIED_KW = [
    "thank you for applying", "we received your application",
    "application has been received", "application confirmation",
    "application submitted", "you applied", "your application to",
    "confirming your application", "application acknowledgement",
    "application acknowledgment", "we have received your application",
    "received your application", "thanks for your application",
    "thank you for your application", "your interest in",
    "your application was received",
]

REJECTION_FETCH_KW = [
    "unfortunately", "not selected", "not moving forward",
    "not been shortlisted", "not been selected to proceed",
    "will not be moving forward", "we will not be moving forward",
    "will not be moving forward with your application",
    "decided to move forward with other candidates",
    "move forward with other candidates", "moving forward with other candidates",
    "other candidates", "not be proceeding", "will not proceed",
    "decided not to proceed", "unable to move forward",
    "no longer under consideration", "regret to inform",
    "application was unsuccessful", "application is unsuccessful",
    "update on your application", "application update",
]

INTERVIEW_FETCH_KW = [
    "interview", "phone screen", "assessment", "schedule a call",
    "schedule an interview", "schedule your interview",
]

OFFER_FETCH_KW = [
    "pleased to offer", "we are offering you", "offer letter",
    "job offer received", "congratulations on your offer",
    "employment agreement", "compensation package", "compensation details",
    "next steps for onboarding", "onboarding documents",
]

OFFER_FETCH_PATTERNS = [
    r"\b(?:pleased|delighted|excited|happy) to offer (?:you|the)\b",
    r"\b(?:formal|official|written|employment|job) offer\b(?!s)",
    r"\boffer letter\b",
    r"\bemployment agreement\b",
    r"\bcompensation (?:package|details|summary)\b",
    r"\b(?:salary|hourly rate|pay rate)\b[\s\S]{0,160}\b(?:offer|employment agreement|start date)\b",
    r"\bcongratulations\b[\s\S]{0,160}\b(?:offer|selected|welcome to|join(?:ing)? our team)\b",
    r"\bnext steps for onboarding\b",
    r"\bonboarding (?:documents|paperwork|portal|process|next steps)\b",
]

RECEIVED_APPLICATION_KW = APPLIED_KW + REJECTION_FETCH_KW + INTERVIEW_FETCH_KW + OFFER_FETCH_KW
RECEIVED_APPLICATION_PATTERNS = OFFER_FETCH_PATTERNS

SENT_APPLICATION_KW = [
    "application for", "applying for", "i am applying", "i'm applying",
    "i would like to apply", "i am writing to apply", "please find attached",
    "attached is my resume", "resume attached", "my resume and cover letter",
    "cover letter and resume", "interest in the position", "interested in the position",
]

SKIP_KW = [
    "jobs you might like", "recommended jobs", "job alert",
    "jobs matching your profile", "new jobs for you", "based on your profile",
    "you might be interested", "check out these jobs", "explore opportunities",
    "relevant vacancies", "new vacancies", "job subscriptions",
    "algorithm matched for you", "job search with jobsora", "jobsora",
    "set up the mail listing", "matched the top relevant queries",
    "newsletter", "marketing preferences", "email preferences",
    "promotional", "promotion", "50% off", "limited time offer",
    "special offer", "resume package", "premium resume",
    "recommended courses", "join pro", "career tips", "upcoming employer events",
    "employer events", "career fair", "webinar", "info session",
    "join your first contest", "leetcode", "coding contest", "win 20",
]

NOISE_EMAIL_PATTERNS = [
    r"\bjob alert\b",
    r"\brecommended jobs\b",
    r"\brecommended (?:roles|opportunities|vacancies)\b",
    r"\brelevant vacancies\b",
    r"\bjobs? you might like\b",
    r"\bnew jobs for you\b",
    r"\bmatched .* for you\b",
    r"\bnewsletter\b",
    r"\bmarketing\b",
    r"\bpromo(?:tional|tion)?\b",
    r"\b\d+% off\b",
    r"\bupcoming employer events\b",
    r"\bcareer fair\b",
    r"\bjoin your first contest\b",
    r"\bcoding contest\b",
    r"\bcontest and win\b",
    r"\bwin \$?\d+\b",
]

INCOMPLETE_APPLICATION_PATTERNS = [
    r"\bincomplete application\b",
    r"\bapplication (?:is|was|remains|appears to be) incomplete\b",
    r"\byour application (?:is|was|remains|appears to be) incomplete\b",
    r"\b(?:your|recent) application\b[\s\S]{0,120}\bis incomplete\b",
    r"\byour recent application with\b[\s\S]{0,120}\bincomplete\b",
    r"\byour application has not been (?:completed|submitted)\b",
    r"\byou (?:have not|haven't|did not|didn't) (?:complete|submit) your application\b",
    r"\bfinish your application\b",
    r"\bcomplete your application\b",
    r"\bcontinue your application\b",
    r"\bresume your application\b",
    r"\breturn to your application\b",
    r"\byou started (?:an|your) application\b",
    r"\bdraft application\b",
    r"\bapplication (?:has )?not (?:been )?submitted\b",
    r"\bbefore we can continue in our hiring process\b",
    r"\bbefore we can continue\b[\s\S]{0,120}\b(?:need|provide|complete|information)\b",
    r"\bwe need (?:a little )?more information\b",
    r"\bfinal reminder\b[\s\S]{0,120}\b(?:complete|information|application)\b",
]

ADMIN_FOLLOWUP_SUBJECT_PATTERNS = [
    r"\bplease verify your identity\b",
    r"\bverify your identity\b",
    r"\bidentity verification\b",
    r"\bverify your email(?: address)?\b",
    r"\bconfirm your email(?: address)?\b",
    r"\baccount creation confirmation\b",
    r"\byour account (?:at|with|for).{0,80}\bwas created\b",
    r"\byour account was created\b",
    r"\baccount (?:has been )?created\b",
    r"\bactivate your account\b",
    r"\bconfirm your account\b",
    r"\bcomplete your profile\b",
    r"\bcreate your profile\b",
    r"\bupdate your profile\b",
    r"\bcomplete your candidate profile\b",
    r"\bcandidate home account\b",
    r"\bportal account\b",
    r"\blogin details\b",
    r"\bkeep track of your application\b",
    r"\btrack your application\b",
    r"\btrack your application status\b",
    r"\bapplication status\b",
    r"\bview your application\b",
    r"\bmanage your application\b",
]

ADMIN_FOLLOWUP_BODY_PATTERNS = [
    r"\bplease verify your identity\b",
    r"\bverify your email(?: address)?\b",
    r"\bconfirm your email(?: address)?\b",
    r"\bactivate your account\b",
    r"\bcomplete your profile\b",
    r"\byour username is\b",
    r"\bcareer site link\b",
    r"\btemporary password\b",
    r"\bkeep track of your application\b",
    r"\btrack your application\b",
    r"\bapplication status\b",
    r"\bcandidate portal\b",
    r"\bview your application\b",
    r"\bmanage your application\b",
]

DISTINCT_APPLICATION_CONFIRMATION_PATTERNS = [
    r"\bthank you for applying (?:to|for)\b",
    r"\bthanks for applying (?:to|for)\b",
    r"\bthank you for your application (?:to|for|at)\b",
    r"\bwe (?:have )?received your application (?:to|for)\b",
    r"\byour application (?:to|for).{0,140}\b(?:has been|was) received\b",
    r"\byour application was received\b",
    r"\bconfirmation of application received for\b",
    r"\byour recent application to\b",
    r"\byour job application for\b",
]

def b64decode(s):
    return base64.urlsafe_b64decode(s + "==").decode("utf-8", errors="ignore")

def extract_body(payload):
    out = []

    def walk(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if mime == "text/plain" and body.get("data"):
            out.append(b64decode(body["data"]))
        elif mime == "text/html" and body.get("data") and not out:
            out.append(re.sub(r"<[^>]+>", " ", b64decode(body["data"])))
        for sub in part.get("parts", []):
            walk(sub)

    if payload.get("body", {}).get("data"):
        out.append(b64decode(payload["body"]["data"]))
    else:
        walk(payload)

    return html.unescape(" ".join(out))[:2500]

def get_header(headers, name):
    return next((h["value"] for h in headers if h["name"].lower() == name), "")

def text_matches_any(patterns, text):
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)

def email_text(email):
    return " ".join([
        email.get("subject", ""),
        email.get("body", ""),
        email.get("snippet", ""),
    ]).lower()

def is_noise_email(text):
    return any(k in text for k in SKIP_KW) or text_matches_any(NOISE_EMAIL_PATTERNS, text)

def is_incomplete_application(text):
    return text_matches_any(INCOMPLETE_APPLICATION_PATTERNS, text)

def has_distinct_application_confirmation(text):
    return text_matches_any(DISTINCT_APPLICATION_CONFIRMATION_PATTERNS, text)

def is_admin_followup(subject, text):
    subject = subject.lower()
    subject_is_admin = text_matches_any(ADMIN_FOLLOWUP_SUBJECT_PATTERNS, subject)
    body_is_admin = text_matches_any(ADMIN_FOLLOWUP_BODY_PATTERNS, text)
    if not subject_is_admin and not body_is_admin:
        return False
    return not has_distinct_application_confirmation(text)

def is_real_offer(text):
    return text_matches_any(OFFER_FETCH_PATTERNS, text)

def display_date(d):
    return d.strftime("%B %d, %Y").replace(" 0", " ")

def parse_start_date(value):
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        sys.exit(f"Invalid --start-date {value!r}. Use YYYY-MM-DD, e.g. 2026-05-08.")

def gmail_date(d):
    return d.strftime("%Y/%m/%d")

def build_gmail_query(start_date):
    # Search from the previous day, then filter parsed email dates below to keep start_date inclusive.
    after_date = start_date - dt.timedelta(days=1)
    return (
        f'after:{gmail_date(after_date)} '
        '(application OR applied OR "your application" OR "thank you for applying" '
        'OR "application received" OR interview OR assessment OR resume OR "cover letter" '
        'OR unfortunately OR "not selected" OR "not moving forward" OR "other candidates" '
        'OR "no longer under consideration" OR "regret to inform" OR "application update" '
        'OR "update on your application" OR "your interest in" OR "incomplete application" '
        'OR "complete your application" OR "finish your application" OR "offer letter" '
        'OR "pleased to offer" OR "job offer")'
    )

def email_is_on_or_after(date_header, start_date):
    try:
        return parsedate_to_datetime(date_header).date() >= start_date
    except Exception:
        return True

def load_email_cache(start_date):
    if not EMAIL_CACHE.exists():
        return None

    raw = json.loads(EMAIL_CACHE.read_text())
    if isinstance(raw, dict):
        if raw.get("version") == CACHE_VERSION and raw.get("start_date") == start_date.isoformat():
            emails = raw.get("emails", [])
            print(f"📦 Using cached {len(emails)} emails  (--refresh to re-fetch)")
            return emails
        if raw.get("version") != CACHE_VERSION:
            print("📦 Ignoring old cache version and re-fetching with the current filters")
            return None
        print(f"📦 Ignoring cache for {raw.get('start_date', 'unknown date')} and re-fetching from {start_date}")
        return None

    print("📦 Ignoring old cache format and re-fetching with the current date filter")
    return None

def save_email_cache(emails, start_date, query):
    EMAIL_CACHE.write_text(json.dumps({
        "version": CACHE_VERSION,
        "start_date": start_date.isoformat(),
        "query": query,
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "emails": emails,
    }, indent=2))

def load_audit_email_cache(start_date):
    if not EMAIL_AUDIT_CACHE.exists():
        return None

    raw = json.loads(EMAIL_AUDIT_CACHE.read_text())
    if isinstance(raw, dict):
        if raw.get("version") == CACHE_VERSION and raw.get("start_date") == start_date.isoformat():
            emails = raw.get("emails", [])
            print(f"📦 Using cached {len(emails)} audit emails  (--refresh to re-fetch)")
            return emails
        if raw.get("version") != CACHE_VERSION:
            print("📦 Ignoring old audit cache version and re-fetching")
            return None
        print(f"📦 Ignoring audit cache for {raw.get('start_date', 'unknown date')} and re-fetching from {start_date}")
        return None

    print("📦 Ignoring old audit cache format and re-fetching")
    return None

def save_audit_email_cache(emails, start_date, query):
    EMAIL_AUDIT_CACHE.write_text(json.dumps({
        "version": CACHE_VERSION,
        "start_date": start_date.isoformat(),
        "query": query,
        "filter_mode": "all",
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "emails": emails,
    }, indent=2))

def load_class_cache():
    if not CLASS_CACHE.exists():
        return {}
    try:
        raw = json.loads(CLASS_CACHE.read_text())
    except Exception:
        return {}
    if raw.get("version") != CLASS_CACHE_VERSION:
        return {}
    return raw.get("items", {})

def save_class_cache(cache):
    CLASS_CACHE.write_text(json.dumps({
        "version": CLASS_CACHE_VERSION,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "items": cache,
    }, indent=2))

def fetch_emails(service, query, start_date, max_candidates=MAX_CANDIDATE_EMAILS, filter_mode="tracker"):
    """Fetch Gmail messages matching query since start_date.

    filter_mode:
      - \"tracker\" (default): apply noise/incomplete/admin/keyword filters
      - \"all\": date cutoff only — full candidate pool for audit/QA labeling
    """
    unfiltered = filter_mode == "all"
    msgs, page_token = [], None
    hit_cap = False

    print(f"🔎 Searching Gmail from {display_date(start_date)} onward, including sent and received mail...")

    while True:
        kwargs = dict(userId="me", q=query, maxResults=100)
        if page_token:
            kwargs["pageToken"] = page_token
        r = service.users().messages().list(**kwargs).execute()
        msgs += r.get("messages", [])
        page_token = r.get("nextPageToken")
        if max_candidates and len(msgs) >= max_candidates:
            msgs = msgs[:max_candidates]
            hit_cap = True
            break
        if not page_token:
            break

    if unfiltered:
        print(f"📩 {len(msgs)} candidate emails — loading unfiltered pool...")
    else:
        print(f"📩 {len(msgs)} candidate emails — filtering...")
    if hit_cap:
        print(f"⚠️  Stopped at --max-candidates={max_candidates}; increase it if you need older matches.")

    results = []
    skipped_incomplete = 0
    skipped_admin = 0
    for i in range(0, len(msgs), 10):
        batch = msgs[i:i+10]
        for m in batch:
            try:
                d = service.users().messages().get(userId="me", id=m["id"], format="full").execute()
            except Exception:
                continue
            labels = set(d.get("labelIds", []))
            direction = "sent" if "SENT" in labels else "received"

            headers = d.get("payload", {}).get("headers", [])
            subject = get_header(headers, "subject")
            from_   = get_header(headers, "from")
            date    = get_header(headers, "date")
            body    = extract_body(d.get("payload", {}))
            snippet = d.get("snippet", "")
            text    = email_text({"subject": subject, "body": body, "snippet": snippet})

            if not email_is_on_or_after(date, start_date):
                continue
            if not unfiltered:
                if is_noise_email(text):
                    continue
                if is_incomplete_application(text):
                    skipped_incomplete += 1
                    continue
                if is_admin_followup(subject, text):
                    skipped_admin += 1
                    continue
                wanted_keywords = SENT_APPLICATION_KW if direction == "sent" else RECEIVED_APPLICATION_KW
                wanted_patterns = [] if direction == "sent" else RECEIVED_APPLICATION_PATTERNS
                if not any(k in text for k in wanted_keywords) and not text_matches_any(wanted_patterns, text):
                    continue

            results.append({
                "id": d["id"],
                "subject": subject,
                "from": from_,
                "date": date,
                "body": body,
                "snippet": snippet,
                "direction": direction,
                "labels": sorted(labels),
            })

        done = min(i + 10, len(msgs))
        print(f"\r  scanned {done}/{len(msgs)}", end="", flush=True)

    if unfiltered:
        print(f"\n✅ {len(results)} unfiltered candidate emails (audit pool)\n")
    else:
        if skipped_incomplete:
            print(f"\n↪️  Skipped {skipped_incomplete} incomplete/not-submitted application emails")
        if skipped_admin:
            print(f"\n↪️  Skipped {skipped_admin} admin/account/identity follow-up emails")
        print(f"\n✅ {len(results)} real application emails\n")
    return results


def fetch_emails_raw(service, query, start_date, max_candidates=MAX_CANDIDATE_EMAILS):
    """Unfiltered candidate pool since start_date (alias for filter_mode=\"all\")."""
    return fetch_emails(service, query, start_date, max_candidates=max_candidates, filter_mode="all")

# ── LLM Classification ────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are classifying job application emails for a 2nd-year Electrical Engineering student at York University.
Emails may be inbound employer messages or outbound messages the student sent to employers.

Return ONLY a JSON object — no markdown, no explanation:
{
  "company":  "Company name",
  "role":     "Job title",
  "status":   "applied" | "assessment_requested" | "recruiter_contact" | "interviewing" | "final_round" | "offer" | "rejected" | "needs_review",
  "confidence": 0.0-1.0,
  "status_scores": {
    "applied": 0.0-1.0,
    "assessment_requested": 0.0-1.0,
    "recruiter_contact": 0.0-1.0,
    "interviewing": 0.0-1.0,
    "final_round": 0.0-1.0,
    "offer": 0.0-1.0,
    "rejected": 0.0-1.0,
    "needs_review": 0.0-1.0
  },
  "summary":  "1-2 sentence plain-English summary of what this email says",
  "location": "City, Province or null"
}

Status rules:
- applied      = application confirmed/waiting, or outbound application email sent to an employer
- assessment_requested = video interview, async interview, test, take-home, technical challenge, screening questions, online assessment, or task requested
- recruiter_contact = recruiter/hiring team asks for availability, more info, screening call, or starts a human follow-up that is not yet an interview
- interviewing = actual interview/phone screen scheduled, requested, or calendar/time selection for interview
- final_round = final interview, final stage, onsite/final panel, or near-offer final process
- rejected     = explicitly told not moving forward. Use only with strong evidence.
- offer        = actual offer only. Require concrete offer language such as "offer letter",
                 "employment agreement", "compensation", "salary/pay rate",
                 "next steps for onboarding", or "congratulations" tied to an offer/selection.
                 Never mark newsletters, job alerts, marketing, recommended jobs,
                 relevant vacancies, or "job offers" ads as offer.
- needs_review = ambiguous, mixed, conditional, automated follow-up, or low confidence.

Bias rules:
- False rejection is worse than false applied. Require high confidence for rejected.
- If confidence is low or wording is ambiguous, use needs_review.
- Do not downgrade assessments/video submissions/take-homes to applied; use assessment_requested."""

gemini_dead = False  # flipped once when quota hit, never checked again
openrouter_dead = False
grok_dead = False
groq_cloud_dead = False
reported_llm_failures = set()
reported_config_warnings = set()

def call_gemini(text, key, model):
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        json={"contents": [{"parts": [{"text": SYSTEM_PROMPT + "\n\nEmail:\n" + text}]}],
              "generationConfig": {
                  "temperature": 0.1,
                  "maxOutputTokens": 500,
                  "responseMimeType": "application/json",
              }},
        timeout=12,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

def call_grok(text, key, model=DEFAULT_GROK_MODEL):
    r = requests.post(
        "https://api.x.ai/v1/chat/completions",
        json={"model": model,
              "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user",   "content": "Email:\n" + text}],
              "temperature": 0.1, "max_tokens": 300},
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=6,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def call_groq_cloud(text, key, model=DEFAULT_GROQ_CLOUD_MODEL):
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        json={"model": model,
              "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": "Email:\n" + text}],
              "temperature": 0.1, "max_tokens": 500,
              "response_format": {"type": "json_object"}},
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=6,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def call_openrouter(text, key, model):
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json={"model": model,
              "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": "Email:\n" + text}],
              "temperature": 0.1, "max_tokens": 500},
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://github.com/jobapp-tracker", "X-Title": "JobAppAITracker"},
        timeout=12,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def parse_json(raw):
    clean = re.sub(r"```json\n?|```", "", raw).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", clean)
        if m:
            return json.loads(m.group())
        raise

def report_llm_failure(name, err):
    if name in reported_llm_failures:
        return
    reported_llm_failures.add(name)

    if isinstance(err, requests.HTTPError) and err.response is not None:
        detail = f"HTTP {err.response.status_code}"
    else:
        detail = f"{type(err).__name__}: {err}"
    print(f"\n⚠️  {name} failed once ({detail}) — using the next classifier.")

def report_config_warning(key, message):
    if key in reported_config_warnings:
        return
    reported_config_warnings.add(key)
    print(f"\n⚠️  {message}")

REJECTION_PATTERNS = [
    r"\bunfortunately\b",
    r"\bwe (?:have )?(?:decided|chosen) to (?:move|proceed) forward with other candidates\b",
    r"\bwe(?:'|')?ve (?:decided|chosen) to (?:move|proceed) forward with other candidates\b",
    r"\b(?:decided|chosen) to (?:move|proceed) forward with other candidates\b",
    r"\bwe (?:will|can) not be moving forward\b",
    r"\bwill not be moving forward with your application\b",
    r"\bwe are (?:unable|not able) to move forward\b",
    r"\bwe (?:have )?decided not to proceed\b",
    r"\byou were not selected\b",
    r"\byou have not been selected\b",
    r"\byou have not been selected to proceed\b",
    r"\byou have not been shortlisted\b",
    r"\byou have not been shortlisted further\b",
    r"\byour application (?:was|has been) not selected\b",
    r"\bnot selected to move forward\b",
    r"\bnot be proceeding\b",
    r"\bwill not proceed\b",
    r"\bnot moving forward\b",
    r"\bno longer under consideration\b",
    r"\bregret to inform\b",
    r"\byour application (?:was|is) unsuccessful\b",
    r"\byour candidacy (?:is|was) no longer\b",
    r"\bmoved? forward with other candidates\b",
    r"\bmoving forward with other candidates\b",
    r"\bpursu(?:e|ing) other candidates\b",
    r"\bhas moved to the next step\b",
]

OFFER_PATTERNS = [
    *OFFER_FETCH_PATTERNS,
]

CONDITIONAL_INTERVIEW_PHRASES = [
    "if your qualifications",
    "if your experience",
    "if your profile",
    "if you are selected",
    "if selected",
    "if selected for an interview",
    "we will contact you",
    "we will reach out",
    "will reach out to you",
    "under review",
    "being reviewed",
    "thoroughly reviewed",
    "align closely with",
    "align with the requirements",
    "look forward to the possibility",
    "at a later time",
    "in the future",
    "under consideration",
    "please be assured",
    "will be reviewed",
]

WEAK_INTERVIEW_PATTERNS = [
    r"\bschedule (?:an|your) interview\b",
    r"\bschedule a call\b",
]

REAL_INTERVIEW_PATTERNS = [
    r"\binvite(?:d)? you to (?:an )?interview\b",
    r"\binterview invitation\b",
    r"\bbook (?:an|your) interview\b",
    r"\bplease (?:book|schedule) your interview\b",
    r"\bselected for (?:an )?interview\b",
    r"\bwould like to interview\b",
    r"\bphone screen\b",
    r"\bassessment (?:invitation|link|request)\b",
    r"\bcomplete (?:the|an|your)?\s*(?:online )?assessment\b",
    r"\bselect (?:a|your) time\b",
    r"\bpick (?:a|your) time\b",
    r"\bchoose (?:a|your) time\b",
    r"\bschedule an interview with you\b",
    r"\bcalendar link\b",
    r"\binterview (?:is )?schedul(?:ed|ing)\b",
]

INTERVIEW_PATTERNS = [
    *WEAK_INTERVIEW_PATTERNS,
    *REAL_INTERVIEW_PATTERNS,
]

ASSESSMENT_REQUEST_PATTERNS = [
    r"\brecord (?:a|your) video\b",
    r"\bvideo (?:interview|submission|assessment|response)\b",
    r"\basync(?:hronous)? interview\b",
    r"\bone[- ]way interview\b",
    r"\btechnical (?:challenge|assessment|test|exercise)\b",
    r"\btake[- ]home\b",
    r"\bcoding (?:challenge|assessment|test|exercise)\b",
    r"\bcomplete (?:the|an|your)?\s*(?:online )?assessment\b",
    r"\bassessment (?:invitation|link|request|due|deadline)\b",
    r"\bcomplete the following questions\b",
    r"\bscreening questions\b",
    r"\bsubmit (?:a|your) (?:video|assignment|assessment|challenge)\b",
]

RECRUITER_CONTACT_PATTERNS = [
    r"\brecruiter\b[\s\S]{0,140}\b(?:reach out|contact|call|availability|available)\b",
    r"\b(?:send|share|provide) (?:your )?availability\b",
    r"\bare you available\b",
    r"\bavailability for (?:a )?(?:call|chat|conversation|screen)\b",
    r"\bquick (?:call|chat|conversation)\b",
    r"\bnext step\b[\s\S]{0,120}\b(?:call|screen|conversation|recruiter)\b",
    r"\bhiring team\b[\s\S]{0,140}\b(?:would like|wants|asked|reach out)\b",
]

FINAL_ROUND_PATTERNS = [
    r"\bfinal (?:round|interview|stage)\b",
    r"\bonsite\b",
    r"\bpanel interview\b",
    r"\blast round\b",
]

STRONG_REJECTION_PATTERNS = [
    r"\b(?:decided|chosen) to (?:move|proceed) forward with other candidates\b",
    r"\bnot (?:been )?(?:selected|shortlisted)\b",
    r"\bnot selected to move forward\b",
    r"\bnot moving forward\b",
    r"\bwill not be moving forward\b",
    r"\bno longer under consideration\b",
    r"\bregret to inform\b",
    r"\bapplication (?:was|is) unsuccessful\b",
    r"\bnot be proceeding\b",
]

def is_real_interview_stage(text):
    t = text.lower()
    has_conditional = any(phrase in t for phrase in CONDITIONAL_INTERVIEW_PHRASES)
    if has_conditional:
        return False
    if text_matches_any(REAL_INTERVIEW_PATTERNS, t):
        return True
    return matches_any(INTERVIEW_PATTERNS, t)

ROLE_PATTERNS = [
    r"applying for (?:the )?(.+?)(?: position| role| job| at | with |\.|,|\n)",
    r"application for (?:the )?(.+?)(?: position| role| job| at | with |\.|,|\n)",
    r"application to (?:the )?(.+?)(?: position| role| job| at | with |\.|,|\n)",
    r"received your application for (?:the )?(.+?)(?: position| role| job| at | with |\.|,|\n)",
    r"position of (.+?)(?:\.|,|\n| and are| with | at )",
    r"role of (.+?)(?:\.|,|\n| and | at )",
]

# ── Company extraction ─────────────────────────────────────────────────────────
#
# Patterns are ordered from most-reliable to least-reliable.
# The extractor tries them in order and returns the first confident hit.
# A "confident hit" is a non-empty result that passes is_bad_company_candidate().

# Tier 1 — short company-name anchors ("at AMD", "joining Google")
# These match the actual company name when the body says "position at X".
COMPANY_PATTERNS_TIER1 = [
    # "at <Company>" when directly preceded by role/position language.
    # Requires a capital-letter start so "at our office" won't match.
    r"(?:position|role|opportunity|opening|job|internship|co-op|coop)\s+at\s+([A-Z][A-Za-z0-9&.,''\-/() ]{1,60}?)(?:\s*[,!.?]|\s+(?:and|for|in|is|has|will|we|our|you|your|where|which|that|as|to)\b)",
    # "joining <Company>" — capital start, NOT followed by possessive/generic determiners.
    # Blocks: "joining the team", "joining our team", "joining us"
    r"\bjoining\s+(?!(?:the|our|your|this|a|an|us)\b)([A-Z][A-Za-z0-9&.,''\-/() ]{1,60}?)(?:\s*[,!.?]|\s+(?:as|for|is|has|and|where|to)\b)",
    # "interest in <Company>" — capital start, blocks "interest in our/the/a …"
    r"\binterest in\s+(?!(?:the|our|your|this|a|an)\b)([A-Z][A-Za-z0-9&.,''\-/() ]{2,50}?)\s+(?:and|for|is|has|will|,|\.|!)",
    # "with <Company> for the … position"
    r"\bwith\s+(?!(?:the|our|your|this|a|an|us)\b)([A-Z][A-Za-z0-9&.,''\-/() ]{2,50}?)\s+for\s+the\s+.{0,120}?\s+position\b",
    # "application with <Company>"
    r"\bapplication\s+with\s+(?!(?:the|our|your|this|a|an)\b)([A-Z][A-Za-z0-9&.,''\-/() ]{2,60}?)(?:\s*[,!.?]|\s+(?:and|for|is|has|will)\b)",
]

# Tier 2 — classic "thank you for applying to X" / "we received your application to X"
COMPANY_PATTERNS_TIER2 = [
    r"\bthank you for applying to ([A-Z][A-Za-z0-9&.,''/() -]+?)(?:!|\.|,|\n| for\b)",
    r"\bthanks for applying to ([A-Z][A-Za-z0-9&.,''/() -]+?)(?:!|\.|,|\n| for\b)",
    r"\bthank you for your application (?:to|at) ([A-Z][A-Za-z0-9&.,''/() -]+?)(?:!|\.|,|\n| for\b)",
    r"\bthank you for taking the time to apply to ([A-Z][A-Za-z0-9&.,''/() -]+?)(?:!|\.|,|\n| for\b)",
    r"\bwe (?:have )?received your application (?:to|at) ([A-Z][A-Za-z0-9&.,''/() -]+?)(?:!|\.|,|\n| for\b)",
    r"\bapplication (?:to|at) ([A-Z][A-Za-z0-9&.,''/() -]+?)(?:!|\.|,|\n| for\b)",
    r"\bapplication to ([A-Z][A-Za-z0-9&.,''/() -]+?)\s+(?:and you can|and we|position|role|with|hi dheeran|dear dheeran)\b",
    r"\bapplication for (?:the )?position .{0,140}? at ([A-Z][A-Za-z0-9&.,''/() -]+?)(?:\s+and you can|\s+and we|!|\.|,|\n)",
    r"\bsubmitting your application for (?:the )?position .{0,140}? at ([A-Z][A-Za-z0-9&.,''/() -]+?)(?:\s+and you can|\s+and we|!|\.|,|\n)",
    r"\b(?:applying|apply|application) for (?:the )?.{0,140}? position at ([A-Z][A-Za-z0-9&.,''/() -]+?)(?: in\b|\.|,|\n)",
    r"\b(?:applying|apply|application) for (?:the )?.{0,140}? role at ([A-Z][A-Za-z0-9&.,''/() -]+?)(?: in\b|\.|,|\n)",
    r"\b(?:posting|opening|opportunity) at ([A-Z][A-Za-z0-9&.,''/() -]+?)(?: in\b|\.|,|\n)",
    r"\bposition with ([A-Z][A-Za-z0-9&.,''/() -]+?)(?:!|\.|,|\n)",
    r"\bwe appreciate your interest in ([A-Z][A-Za-z0-9&.,''/() -]+?)(?:!|\.|,|\n)",
    r"\bhuman resources department\s+([A-Z][A-Za-z0-9&.'\-]{2,40})\s*(?:</|\.|,|\n|\byou\b|$)",
]

# Kept for backward-compat (used nowhere directly now, tiers replace it)
COMPANY_PATTERNS = COMPANY_PATTERNS_TIER2

# ── Company suffix stripping ───────────────────────────────────────────────────
# Applied inside clean_company() to remove ATS/HR boilerplate appended to real names.
# Order matters: more specific patterns first.
COMPANY_SUFFIX_STRIP = [
    r"\s+Group$",
    r"\s+Group\s+Recruiting\s+Team$",
    r"\s+Recruiting\s+Team$",
    r"\s+Talent\s+Acquisition(?:\s+(?:Team|Department))?$",
    r"\s+HR(?:\s+(?:Team|Department))?$",
    r"\s+Human\s+Resources(?:\s+(?:Team|Department))?$",
    r"\s+Careers?$",
    r"\s+Recruiting$",
    r"\s+Recruitment$",
    r"\s+(?:Staffing|Hiring)\s+Team$",
    r"\s+Workday$",
    r"\s+via\s+Workday$",
    r"\s+\|\s*Workday$",
    r"\s+Auto\s*Notification$",
    r"\s+AutoNotification$",
    r"\s+Notifications?$",
    r"\s+No.?Reply$",
    r"\s+noreply$",
]

# ── Company normalization map ──────────────────────────────────────────────────
# Keys are lowercase, spaces-stripped. Values are canonical display names.
COMPANY_NORMALIZE = {
    # Auto / manufacturing
    "generalmotors": "General Motors",
    "gm": "General Motors",
    "ford": "Ford Motor Company",
    "stellantis": "Stellantis",
    "toyota": "Toyota",
    "honda": "Honda",
    "magna": "Magna International",
    "martinrea": "Martinrea International",
    "multimatic": "Multimatic",
    "linamar": "Linamar",
    # Tech
    "amd": "AMD",
    "nvidia": "NVIDIA",
    "intel": "Intel",
    "qualcomm": "Qualcomm",
    "broadcom": "Broadcom",
    "appliedmaterials": "Applied Materials",
    "amat": "Applied Materials",
    "ibm": "IBM",
    "microsoft": "Microsoft",
    "google": "Google",
    "amazon": "Amazon",
    "meta": "Meta",
    "apple": "Apple",
    "samsung": "Samsung",
    "lg": "LG",
    "ericsson": "Ericsson",
    "nokia": "Nokia",
    "siemens": "Siemens",
    "abb": "ABB",
    "bosch": "Bosch",
    "texas instruments": "Texas Instruments",
    "ti": "Texas Instruments",
    "analogdevices": "Analog Devices",
    "adi": "Analog Devices",
    "microchip": "Microchip Technology",
    "stmicroelectronics": "STMicroelectronics",
    "st": "STMicroelectronics",
    "infineon": "Infineon Technologies",
    "nxp": "NXP Semiconductors",
    "renesas": "Renesas",
    "marvell": "Marvell Technology",
    "xilinx": "AMD (Xilinx)",
    # Finance / consulting
    "rbc": "RBC",
    "td": "TD Bank",
    "bmo": "BMO",
    "scotiabank": "Scotiabank",
    "cibc": "CIBC",
    "manulife": "Manulife",
    "sunlife": "Sun Life",
    "pwc": "PwC",
    "deloitte": "Deloitte",
    "kpmg": "KPMG",
    "ey": "EY",
    "mckinsey": "McKinsey",
    "bcg": "BCG",
    # Energy / utilities
    "ontario power generation": "Ontario Power Generation",
    "opg": "Ontario Power Generation",
    "hydro one": "Hydro One",
    "hydroone": "Hydro One",
    "enbridge": "Enbridge",
    "suncor": "Suncor",
    "shell": "Shell",
    "cenovus": "Cenovus Energy",
    # Defense / aerospace
    "l3harris": "L3Harris",
    "l3 harris": "L3Harris",
    "lockheedmartin": "Lockheed Martin",
    "rtx": "RTX",
    "raytheon": "Raytheon",
    "northrop": "Northrop Grumman",
    "bae": "BAE Systems",
    # Other known employers
    "erco": "ERCO",
    "arup": "Arup",
    "wsatkins": "Atkins",
    "atkins": "Atkins",
    "jdirving": "J.D. Irving",
    "j d irving": "J.D. Irving",
    "jdi": "J.D. Irving",
    "jd": "J.D. Irving",
    "j.d": "J.D. Irving",
    "irving": "J.D. Irving",
    "tuvsud": "TÜV SÜD",
    "tüvsüd": "TÜV SÜD",
    "tuvsudgroup": "TÜV SÜD",
    "tüvsüdgroup": "TÜV SÜD",
    "tuv sud": "TÜV SÜD",
    "tuv sud group": "TÜV SÜD",
    "cgi": "CGI Group",
    "tcs": "Tata Consultancy Services",
    "wipro": "Wipro",
    "infosys": "Infosys",
    "accenture": "Accenture",
    "capgemini": "Capgemini",
    "aecom": "AECOM",
    "stantec": "Stantec",
    "wsp": "WSP",
    "hatch": "Hatch",
    "exp": "EXP",
    "parsons": "Parsons",
    "jacobs": "Jacobs Engineering",
}

# Absolute garbage strings that should never be treated as a company name.
# All lowercase, normalized (spaces collapsed).
BAD_COMPANY_SET = {
    "notify",
    "autonotification",
    "auto notification",
    "dayforce",
    "workday",
    "noreply",
    "no-reply",
    "no reply",
    "do-not-reply",
    "do not reply",
    "donotreply",
    "site",
    "group",
    "our",
    "our organization",
    "the organization",
    "our company",
    "the company",
    "our team",
    "the team",
    "a position",
    "us",
    "unknown",
    "none",
    "null",
    "n/a",
    "na",
    "the role",
    "the position",
    "this role",
    "this position",
    "our client",
    "the client",
    "a leading",
    "a global",
    "a fast-growing",
    "a top",
    # Conversational phrases that Tier-1 patterns can accidentally capture
    "joining the team",
    "joining our team",
    "joining your team",
    "joining us",
    "the hiring team",
    "our hiring team",
    "the recruiting team",
    "our recruiting team",
    "the talent team",
    "our talent team",
    "the people team",
    "human resources",
    "hr team",
    "the hr team",
    "our hr team",
    "fit for this",
    "autonotification",
    "auto notification",
    "autonotification workday",
    "auto notification workday",
}

WEAK_COMPANY_NAMES = {
    "notify", "noreply", "no-reply", "no reply", "do-not-reply", "donotreply",
    "autonotification", "auto notification", "dayforce", "workday",
    "workday hr", "auto notification workday", "autonotification workday",
    "system administrator", "administrator", "btalent administrator",
    "collage hr", "recruiting", "talent acquisition team",
    "noreplytrenchrecruiting", "gts workday", "gts",
    "site", "group", "our",
}

GENERIC_EMAIL_DOMAINS = {
    "gmail", "googlemail", "outlook", "hotmail", "live", "icloud", "yahoo",
    "workday", "myworkday", "dayforce", "successfactors", "csod", "smartrecruiters",
    "greenhouse", "lever", "bamboohr", "ultipro", "oraclecloud", "myworkdaysite",
    "taleo",
}

PORTAL_SENDER_PATTERNS = [
    r"workday", r"dayforce", r"autonotification", r"auto[-_]?notification",
    r"csod", r"taleo", r"successfactors", r"noreply", r"no-reply",
    r"do-not-reply", r"donotreply", r"notify",
]

def matches_any(patterns, text):
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)

def clean_role(role):
    role = html.unescape(re.sub(r"\s+", " ", role)).strip(" -:;.,")
    role = re.sub(r"^(?:the\s+)?(?:position|role|job)\s+(?:of\s+)?", "", role, flags=re.IGNORECASE)
    role = re.split(r"\s*(?:<span|#outlook|\{|\bbody\b|font-family|webkit-text|mso-|style=)", role, maxsplit=1, flags=re.IGNORECASE)[0]
    role = re.split(r"\s+(?:and are currently|and we|we have|your resume|if your profile|should your experience)\b", role, maxsplit=1, flags=re.IGNORECASE)[0]
    role = role.strip(" -:;.,")
    return role[:140] if role else "Unknown"

def clean_company(company):
    company = html.unescape(re.sub(r"\s+", " ", company)).strip(" -:;.,\"'")
    company = re.sub(r"^sincerely[,:]?\s+", "", company, flags=re.IGNORECASE)
    company = re.sub(r"\b(?:my)?workday(?:site)?\b", "", company, flags=re.IGNORECASE).strip(" -:;.,\"'")
    company = re.split(
        r"\s+(?:and you can|and we|hi dheeran|dear dheeran|position|role|with)\b",
        company,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    company = re.split(r"[.!?](?:\s|$)", company, maxsplit=1)[0]
    # Strip ATS/HR boilerplate suffixes (e.g. "TÜV SÜD Group Recruiting Team" → "TÜV SÜD")
    for pattern in COMPANY_SUFFIX_STRIP:
        company = re.sub(pattern, "", company, flags=re.IGNORECASE).strip()
    company = re.sub(r"\s+(?:in|at)\s+[A-Z][A-Za-z .'-]+$", "", company).strip()
    return company[:100] if company else ""

def normalize_company(company: str) -> str:
    """Apply the normalization map. Returns the canonical name if found, else the input unchanged."""
    if not company:
        return company
    key = re.sub(r"[^a-z0-9]", "", company.lower())
    if key in COMPANY_NORMALIZE:
        return COMPANY_NORMALIZE[key]
    # Also try with spaces preserved
    key2 = re.sub(r"\s+", " ", company.lower()).strip()
    if key2 in COMPANY_NORMALIZE:
        return COMPANY_NORMALIZE[key2]
    # "TÜV SÜD Group" → strip suffix and retry map lookup
    if re.search(r"\s+group$", company, re.IGNORECASE):
        stripped = re.sub(r"\s+group$", "", company, flags=re.IGNORECASE).strip()
        if stripped and stripped != company:
            mapped = normalize_company(stripped)
            if mapped != stripped:
                return mapped
    return company

def sender_display_name(raw_from):
    m = re.search(r"^\s*\"?([^\"<@]+?)\"?\s*(?:<|$)", raw_from or "")
    return clean_company(m.group(1)) if m else clean_company((raw_from or "").split("@")[0])

def is_weak_company(company):
    normalized = re.sub(r"[^a-z0-9]+", " ", company.lower()).strip()
    if normalized in WEAK_COMPANY_NAMES:
        return True
    weak_tokens = (
        "noreply", "no reply", "do not reply", "donotreply", "notify",
        "autonotification", "auto notification", "dayforce", "workday",
    )
    return any(token in normalized for token in weak_tokens)

def is_bad_company_candidate(company: str) -> bool:
    """Return True if `company` is clearly not a real company name."""
    value = clean_company(company)
    if not value:
        return True

    # Absolute blacklist check
    normalized = re.sub(r"\s+", " ", value.lower()).strip()
    if normalized in BAD_COMPANY_SET:
        return True
    if normalized.startswith("sincerely"):
        return True
    # Starts with a bad prefix
    bad_prefixes = ("our ", "the ", "a ", "an ", "this ")
    for pfx in bad_prefixes:
        if normalized.startswith(pfx) and len(normalized) < 30:
            return True

    letters = len(re.findall(r"[A-Za-z]", value))
    digits = len(re.findall(r"\d", value))
    if digits and digits >= letters:
        return True
    if re.fullmatch(r"[A-Z]{0,3}\d[A-Z0-9-]*", value, re.IGNORECASE):
        return True
    if re.search(r"\b(?:job|req|requisition|id|jr|r)\s*[-#:]*\s*\d", value, re.IGNORECASE):
        return True

    # Temporal or generic descriptor as the first word → job title fragment, not a company
    temporal_prefixes = r"^(?:short|long|full|part|co-op|coop|summer|winter|fall|spring|contract|term|permanent|temporary)\b"
    if re.search(temporal_prefixes, normalized, re.IGNORECASE):
        return True

    # Contains role-like words → probably captured a role, not a company.
    # Threshold is 3+ words so "Engineering Intern" alone doesn't block "AMD Intern Program" etc.
    role_words = r"\b(?:intern|internship|co-op|coop|engineer|analyst|developer|designer|manager|coordinator|associate|assistant|officer|specialist|consultant|technician|representative|administrator|summer|full.time|part.time|contract|temporary|permanent|junior|senior|lead|staff|principal)\b"
    if re.search(role_words, value, re.IGNORECASE) and len(value.split()) >= 3:
        return True

    # Looks like an ATS/requisition code: all caps/digits, no vowels (e.g. ZZG-HRCVC)
    # But skip this check if the name is in our known-good normalization map.
    stripped = re.sub(r"[-_]", "", value)
    norm_key = re.sub(r"[^a-z0-9]", "", value.lower())
    if (
        re.fullmatch(r"[A-Z0-9]{3,}", stripped)
        and not re.search(r"[aeiou]", stripped, re.IGNORECASE)
        and norm_key not in COMPANY_NORMALIZE
    ):
        return True

    return False

def is_portal_sender(raw_from):
    raw = (raw_from or "").lower()
    return any(re.search(pattern, raw) for pattern in PORTAL_SENDER_PATTERNS)

def company_from_domain(raw_from):
    m = re.search(r"@([A-Za-z0-9.-]+)", raw_from or "")
    if not m:
        return ""
    parts = [p for p in m.group(1).lower().split(".") if p]
    if len(parts) < 2:
        return ""
    root = parts[-2]
    if root in GENERIC_EMAIL_DOMAINS:
        return ""
    return clean_company(root.upper() if len(root) <= 4 else root.title())

def _try_tier(patterns, text, min_words=1, max_words=10):
    """Try a list of patterns; return the best non-bad match or None."""
    candidates = []
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            company = clean_company(m.group(1))
            if not company:
                continue
            word_count = len(company.split())
            if word_count < min_words or word_count > max_words:
                continue
            if not is_bad_company_candidate(company):
                candidates.append(company)
    if not candidates:
        return None
    # Prefer shorter names (more likely to be the actual company, not a long phrase)
    return min(candidates, key=lambda c: len(c))

def finalize_company(company):
    """Map generic/weak portal tokens to Unknown; prefer Unknown over wrong names."""
    if not company:
        return "Unknown"
    name = normalize_company(clean_company(company))
    if not name or is_weak_company(name):
        return "Unknown"
    if re.sub(r"\s+", " ", name.lower()).strip() in BAD_COMPANY_SET:
        return "Unknown"
    return name

def _company_extraction_text(email):
    """Plain text for company patterns (HTML tags removed)."""
    parts = [email.get("subject", ""), email.get("body", ""), email.get("snippet", "")]
    text = html.unescape(" ".join(parts))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def extract_company_fallback(email):
    raw_from = email.get("from", "")
    display = sender_display_name(raw_from)
    portal = is_portal_sender(raw_from)

    full_text = _company_extraction_text(email)

    def accept(hit):
        if hit and not is_bad_company_candidate(hit):
            return finalize_company(hit)
        return None

    accepted = accept(_try_tier(COMPANY_PATTERNS_TIER1, full_text, min_words=1, max_words=6))
    if accepted:
        return accepted

    accepted = accept(_try_tier(COMPANY_PATTERNS_TIER2, full_text, min_words=1, max_words=8))
    if accepted:
        return accepted

    domain_company = company_from_domain(raw_from)
    if domain_company and not is_bad_company_candidate(domain_company):
        return finalize_company(domain_company)

    if (
        not portal
        and display
        and not is_weak_company(display)
        and not is_bad_company_candidate(display)
    ):
        return finalize_company(display)

    if (
        portal
        and display
        and not is_weak_company(display)
        and not is_bad_company_candidate(display)
        and len(display.split()) >= 2
        and not re.search(r"\b(?:noreply|notify|workday|dayforce|autonotification)\b", display, re.I)
    ):
        return finalize_company(display)

    return "Unknown"

def extract_role_fallback(email):
    text = " ".join([
        email.get("subject", ""),
        email.get("body", ""),
        email.get("snippet", ""),
    ])
    for pattern in ROLE_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return clean_role(m.group(1))
    return "Unknown"

def empty_status_scores():
    return {status: 0.0 for status in STATUSES}

def clamp_score(value):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0

def normalize_scores(scores):
    out = empty_status_scores()
    if isinstance(scores, dict):
        for status, value in scores.items():
            mapped = map_status(status)
            if mapped in out:
                out[mapped] = max(out[mapped], clamp_score(value))
    return out

def map_status(status):
    status = str(status or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "unknown": "needs_review",
        "review": "needs_review",
        "recruiting_active": "assessment_requested",
        "assessment": "assessment_requested",
        "assessment_sent": "assessment_requested",
        "assessment_request": "assessment_requested",
        "screening": "recruiter_contact",
        "recruiter_screen": "recruiter_contact",
        "phone_screen": "interviewing",
        "interview": "interviewing",
        "final": "final_round",
        "final_interview": "final_round",
    }
    return aliases.get(status, status if status in STATUSES else "needs_review")

def choose_status(scores, direction="received"):
    scores = normalize_scores(scores)
    if not scores or max(scores.values(), default=0.0) <= 0.0:
        return "needs_review", 0.0, scores

    # Positive bias: only strong explicit evidence should beat an active/applied state with rejection.
    priority = ["offer", "final_round", "interviewing", "assessment_requested", "recruiter_contact", "rejected", "applied", "needs_review"]
    status = max(priority, key=lambda s: (scores.get(s, 0.0), -priority.index(s)))
    confidence = scores.get(status, 0.0)

    threshold = STATUS_THRESHOLDS.get(status, 0.80)
    if confidence < threshold:
        if scores.get("applied", 0.0) >= STATUS_THRESHOLDS["applied"]:
            return "applied", scores["applied"], scores
        return "needs_review", max(confidence, scores.get("needs_review", 0.0), 0.50), scores
    return status, confidence, scores

def rule_status_scores(email):
    text = email_text(email)
    direction = email.get("direction", "received")
    scores = empty_status_scores()
    signals = []

    if is_noise_email(text) or is_incomplete_application(text) or is_admin_followup(email.get("subject", ""), text):
        scores["needs_review"] = 0.65
        signals.append("filtered_or_admin_signal")
        return scores, signals

    if direction == "sent":
        if any(k in text for k in SENT_APPLICATION_KW) or re.search(r"\bapplication for .+ position\b", text, re.IGNORECASE):
            scores["applied"] = max(scores["applied"], 0.78)
            signals.append("sent_application")
    else:
        if has_distinct_application_confirmation(text):
            scores["applied"] = max(scores["applied"], 0.78)
            signals.append("application_confirmation")
        elif any(k in text for k in APPLIED_KW):
            scores["applied"] = max(scores["applied"], 0.62)
            signals.append("weak_application_ack")

    if text_matches_any(STRONG_REJECTION_PATTERNS, text):
        scores["rejected"] = max(scores["rejected"], 0.94)
        signals.append("strong_rejection")
    elif matches_any(REJECTION_PATTERNS, text):
        scores["rejected"] = max(scores["rejected"], 0.68)
        scores["needs_review"] = max(scores["needs_review"], 0.72)
        signals.append("weak_rejection_language")

    if is_real_offer(text):
        scores["offer"] = max(scores["offer"], 0.93)
        signals.append("real_offer")

    if text_matches_any(FINAL_ROUND_PATTERNS, text):
        scores["final_round"] = max(scores["final_round"], 0.85)
        signals.append("final_round")
    if text_matches_any(ASSESSMENT_REQUEST_PATTERNS, text):
        scores["assessment_requested"] = max(scores["assessment_requested"], 0.86)
        signals.append("assessment_requested")
    if text_matches_any(RECRUITER_CONTACT_PATTERNS, text):
        scores["recruiter_contact"] = max(scores["recruiter_contact"], 0.80)
        signals.append("recruiter_contact")
    if is_real_interview_stage(text):
        scores["interviewing"] = max(scores["interviewing"], 0.82)
        signals.append("interview_stage")

    if not signals:
        scores["needs_review"] = 0.55
        signals.append("no_clear_signal")

    return scores, signals

def base_result(email, via):
    return {
        "company": extract_company_fallback(email),
        "role": extract_role_fallback(email),
        "summary": html.unescape(email.get("snippet", "")),
        "location": None,
        "_via": via,
    }

def classify_by_rules(email):
    scores, signals = rule_status_scores(email)
    status, confidence, scores = choose_status(scores, email.get("direction", "received"))
    return {
        **base_result(email, "rules"),
        "status": status,
        "confidence": confidence,
        "status_scores": scores,
        "_signals": signals,
    }

def keyword_fallback(email):
    result = classify_by_rules(email)
    result["_via"] = "fallback"
    return result

def normalize_dedupe_text(value):
    value = html.unescape(str(value or "")).lower()
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"\b(?:jr|job|req|requisition|id|r)\s*[-#:]*\s*\d+[a-z0-9-]*\b", " ", value)
    value = re.sub(r"\b\d{4,}\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def dedupe_key(job):
    company = finalize_company(normalize_company(clean_company(job.get("company") or "")))
    company = normalize_dedupe_text(company)
    role = normalize_dedupe_text(job.get("role"))
    if not company or company == "unknown" or not role or role == "unknown":
        return None
    return company, role

def status_rank(status):
    return {
        "offer": 8,
        "final_round": 7,
        "interviewing": 6,
        "assessment_requested": 5,
        "recruiter_contact": 4,
        "rejected": 3,
        "applied": 2,
        "needs_review": 1,
        "unknown": 1,
    }.get(status, 0)

def via_rank(via):
    via = via or ""
    if via.startswith("gemini"):
        return 4
    if via.startswith("grok") or via.startswith("groq") or via.startswith("openrouter"):
        return 3
    if via == "rules":
        return 2
    if via == "fallback":
        return 1
    return 0

def merge_duplicate_jobs(jobs):
    merged = []
    by_key = {}

    for job in jobs:
        key = dedupe_key(job)
        if not key:
            merged.append(job)
            continue

        existing = by_key.get(key)
        if not existing:
            by_key[key] = job
            merged.append(job)
            continue

        duplicate_ids = existing.setdefault("duplicateEmailIds", [])
        duplicate_ids.append(job.get("emailId"))

        directions = set(filter(None, str(existing.get("direction", "")).split("+")))
        directions.update(filter(None, str(job.get("direction", "")).split("+")))
        if directions:
            existing["direction"] = "+".join(sorted(directions))
        merged_direction = existing.get("direction")

        if status_rank(job.get("status")) > status_rank(existing.get("status")):
            existing.update({k: v for k, v in job.items() if k != "duplicateEmailIds"})
            existing["duplicateEmailIds"] = duplicate_ids
            existing["direction"] = merged_direction
        elif status_rank(job.get("status")) == status_rank(existing.get("status")) and via_rank(job.get("_via")) > via_rank(existing.get("_via")):
            for field in ["company", "role", "summary", "location", "_via"]:
                if job.get(field):
                    existing[field] = job[field]

    return merged

def test_ai_classifiers(config):
    email = {
        "from": "Example Careers <careers@example.com>",
        "date": "Mon, 11 May 2026 13:35:06 +0000",
        "subject": "Thank you for your interest in our Electrical Engineering Co-op position",
        "body": (
            "Hi Dheeran, thank you for your interest in our Electrical Engineering Co-op "
            "position. After consideration, we will not be moving forward with your application."
        ),
        "snippet": "After consideration, we will not be moving forward with your application.",
        "direction": "received",
    }
    result = classify(email, config)
    print("\nAI test result:")
    print(json.dumps(result, indent=2))

CLASSIFICATION_FIXTURES = [
    {
        "id": "1-applied-thank-you",
        "expected_status": "applied",
        "subject": "Thank you for applying",
        "body": "Thank you for applying. We have received your application.",
        "snippet": "Thank you for applying",
        "from": "Careers <careers@example.com>",
        "direction": "received",
    },
    {
        "id": "2-rejected-update",
        "expected_status": "rejected",
        "subject": "Application update",
        "body": "Unfortunately, we will not be moving forward with your application.",
        "snippet": "Unfortunately, we will not be moving forward",
        "from": "HR <hr@example.com>",
        "direction": "received",
    },
    {
        "id": "3-interview-invitation",
        "expected_status": "interviewing",
        "subject": "Interview invitation",
        "body": "We would like to schedule an interview with you. Please choose a time.",
        "snippet": "schedule an interview with you. Please choose a time",
        "from": "Recruiting <recruit@example.com>",
        "direction": "received",
    },
    {
        "id": "4-applied-conditional-interview",
        "expected_status": "applied",
        "subject": "Your application was received",
        "body": "Your application was received. If selected for an interview, we will contact you.",
        "snippet": "If selected for an interview, we will contact you",
        "from": "HR <hr@example.com>",
        "direction": "received",
    },
    {
        "id": "5-noise-marketing",
        "expected_status": "needs_review",
        "subject": "Special offer for job seekers",
        "body": "Get our premium resume package today — 50% off limited time offer.",
        "snippet": "premium resume package",
        "from": "Marketing <promo@example.com>",
        "direction": "received",
    },
    {
        "id": "6-offer-employment",
        "expected_status": "offer",
        "subject": "Offer of employment",
        "body": "We are pleased to offer you the position on our team.",
        "snippet": "pleased to offer you",
        "from": "HR <hr@example.com>",
        "direction": "received",
    },
    {
        "id": "7-rejected-dayforce-erco",
        "expected_status": "rejected",
        "expected_company": "ERCO",
        "subject": "Thank you for your recent application",
        "body": (
            "We have carefully reviewed your application for the Electrical Engineering Student "
            "position. Unfortunately, you have not been selected to proceed in the recruitment "
            "process. Thank you for your interest and we would like to encourage you to continue "
            "visiting our careers page. This e-mail box is not monitored. Please do not reply to "
            "this message. Human Resources Department ERCO"
        ),
        "snippet": "you have not been selected to proceed",
        "from": "notify@dayforce.com",
        "direction": "received",
    },
    {
        "id": "8-assessment-video",
        "expected_status": "assessment_requested",
        "subject": "Next step for your application",
        "body": "Please record a video response and complete the following questions by Friday.",
        "snippet": "record a video response and complete the following questions",
        "from": "Hiring Team <hiring@example.com>",
        "direction": "received",
    },
]

def classify_fixture(email):
    """Score-based fallback path without calling LLM APIs."""
    return apply_guardrails(keyword_fallback(email), email)

def coerce_classifier_result(result, email):
    result = dict(result or {})
    rule_scores, signals = rule_status_scores(email)
    llm_scores = normalize_scores(result.get("status_scores") or result.get("scores"))
    mapped_status = map_status(result.get("status"))

    if not any(llm_scores.values()):
        llm_confidence = clamp_score(result.get("confidence"))
        if not llm_confidence:
            llm_confidence = 0.60 if mapped_status == "needs_review" else 0.78
            if mapped_status in ACTIVE_STATUSES:
                llm_confidence = 0.86
            if mapped_status in {"rejected", "offer"}:
                llm_confidence = 0.88
        llm_scores[mapped_status] = llm_confidence

    scores = empty_status_scores()
    for status in STATUSES:
        scores[status] = max(rule_scores.get(status, 0.0), llm_scores.get(status, 0.0))

    status, confidence, scores = choose_status(scores, email.get("direction", "received"))
    result["status"] = status
    result["confidence"] = confidence
    result["status_scores"] = scores
    if signals:
        result["_signals"] = sorted(set(result.get("_signals", []) + signals))
    return result

def apply_guardrails(result, email):
    text = email_text(email)
    result = coerce_classifier_result(result, email)
    status = result.get("status")

    # Clean and validate company
    raw_company = result.get("company") or ""
    result["company"] = clean_company(raw_company)

    # If cached/LLM company is weak or bad, override with tier/domain extractor (not portal display names)
    if (
        not result["company"]
        or is_weak_company(result["company"])
        or is_bad_company_candidate(result["company"])
        or re.sub(r"\s+", " ", result["company"].lower()).strip() in BAD_COMPANY_SET
    ):
        fallback_company = extract_company_fallback(email)
        if fallback_company and fallback_company != "Unknown":
            result["company"] = fallback_company
        elif not result["company"]:
            result["company"] = fallback_company or "Unknown"

    result["company"] = finalize_company(normalize_company(result.get("company") or ""))

    # Clean and validate role
    result["role"] = clean_role(result.get("role") or "")
    if not result["role"] or result["role"].lower() in {"none", "null", "unknown"}:
        result["role"] = extract_role_fallback(email)

    if is_noise_email(text):
        result["status"] = "needs_review"
        result["confidence"] = min(result.get("confidence", 0.0), 0.55)
        result["_guardrail"] = "noise_email"
        return result

    if status == "offer" and not is_real_offer(text):
        scores = normalize_scores(result.get("status_scores"))
        scores["offer"] = 0.0
        result["status"], result["confidence"], result["status_scores"] = choose_status(scores, email.get("direction", "received"))
        result["_guardrail"] = "offer_requires_offer_letter_compensation_or_onboarding"

    return result

def classify(email, config):
    global gemini_dead, openrouter_dead, grok_dead, groq_cloud_dead

    text = (
        f"Direction: {email.get('direction', 'received')}\n"
        f"From: {email['from']}\nDate: {email['date']}\n"
        f"Subject: {email['subject']}\n\n{email['body']}"
    )

    gemini_key = config_value(config, "gemini_api_key")
    tried_gemini = False
    if gemini_key and not gemini_dead:
        tried_gemini = True
        for model in config_list(config, "gemini_models", DEFAULT_GEMINI_MODELS):
            try:
                return apply_guardrails(
                    {**parse_json(call_gemini(text, gemini_key, model)), "_via": f"gemini:{model}"},
                    email,
                )
            except json.JSONDecodeError as e:
                report_llm_failure(f"Gemini {model}", e)
                continue
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 404:
                    report_llm_failure(f"Gemini {model}", e)
                    continue
                if status == 429:
                    print("\n⚠️  Gemini quota hit — trying OpenRouter / other classifiers.")
                    gemini_dead = True
                    break
                report_llm_failure(f"Gemini {model}", e)
                break
            except Exception as e:
                report_llm_failure(f"Gemini {model}", e)
                break

    openrouter_key = config_value(config, "openrouter_api_key")
    if openrouter_key and not openrouter_dead and (gemini_dead or not gemini_key or tried_gemini):
        openrouter_model = config.get("openrouter_model", DEFAULT_OPENROUTER_MODEL)
        try:
            return apply_guardrails(
                {**parse_json(call_openrouter(text, openrouter_key, openrouter_model)),
                 "_via": f"openrouter:{openrouter_model}"},
                email,
            )
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 429:
                openrouter_dead = True
            report_llm_failure(f"OpenRouter {openrouter_model}", e)
        except Exception as e:
            report_llm_failure(f"OpenRouter {openrouter_model}", e)

    xai_key = config_value(config, "grok_api_key", "xai_api_key")
    groq_cloud_key = config_value(config, "groq_api_key")

    if not xai_key and groq_cloud_key and str(groq_cloud_key).startswith("xai-"):
        xai_key = groq_cloud_key
        report_config_warning(
            "groq_xai_alias",
            "Using config key 'groq_api_key' as an xAI/Grok key because it starts with 'xai-'. Prefer renaming it to 'grok_api_key'."
        )

    if xai_key and not grok_dead:
        if str(xai_key).startswith("gsk_"):
            report_config_warning(
                "grok_key_provider",
                "Your Grok/xAI key looks like a Groq Cloud key. Skipping xAI/Grok and trying Groq Cloud instead."
            )
            grok_dead = True
        else:
            grok_model = config.get("grok_model", DEFAULT_GROK_MODEL)
            try:
                return apply_guardrails(
                    {**parse_json(call_grok(text, xai_key, grok_model)), "_via": f"grok:{grok_model}"},
                    email,
                )
            except Exception as e:
                report_llm_failure(f"Grok {grok_model}", e)
                grok_dead = True
                print("\n⚠️  Grok failed — using the next classifier for the rest of this run.")

    if groq_cloud_key and not groq_cloud_dead and not str(groq_cloud_key).startswith("xai-"):
        groq_model = config.get("groq_model", DEFAULT_GROQ_CLOUD_MODEL)
        try:
            return apply_guardrails(
                {**parse_json(call_groq_cloud(text, groq_cloud_key, groq_model)), "_via": f"groq:{groq_model}"},
                email,
            )
        except Exception as e:
            report_llm_failure(f"Groq Cloud {groq_model}", e)
            groq_cloud_dead = True
            print("\n⚠️  Groq Cloud failed — using keyword fallback for the rest of this run.")

    return apply_guardrails(keyword_fallback(email), email)

# ── Terminal Output ───────────────────────────────────────────────────────────
ICONS = {
    "offer": "🎉",
    "final_round": "🏁",
    "interviewing": "🎯",
    "assessment_requested": "🧪",
    "recruiter_contact": "☎️",
    "applied": "📋",
    "rejected": "❌",
    "needs_review": "❓",
    "unknown": "❓",
}

def print_results(jobs):
    by_status = {status: [] for status in ["offer", "final_round", "interviewing", "assessment_requested", "recruiter_contact", "applied", "rejected", "needs_review"]}
    for j in jobs:
        by_status.setdefault(j.get("status", "needs_review"), by_status["needs_review"]).append(j)

    print("\n" + "─" * 60)
    print("  JOB APPLICATION TRACKER — York EE")
    print("─" * 60)

    for status, lst in by_status.items():
        if not lst:
            continue
        icon = ICONS.get(status, "❓")
        print(f"\n{icon}  {status.upper()} ({len(lst)})")
        print("  " + "·" * 40)
        for j in lst:
            print(f"  🏢 {j['company']}  |  {j['role']}")
            print(f"     {j['summary']}")
            if j.get("location"):
                print(f"     📍 {j['location']}")
            direction = j.get("direction", "received")
            confidence = j.get("confidence")
            conf_part = f" / {round(confidence * 100)}%" if isinstance(confidence, (int, float)) else ""
            print(f"     [{direction} / {j.get('_via', '?')}{conf_part}]  {j.get('rawDate', '')}")

    total       = len(jobs)
    active_count = sum(len(by_status[s]) for s in ["assessment_requested", "recruiter_contact", "interviewing", "final_round"])
    rejected     = len(by_status["rejected"])
    applied      = len(by_status["applied"])
    offers       = len(by_status["offer"])
    needs_review = len(by_status["needs_review"])
    sent_count   = sum(1 for j in jobs if "sent" in str(j.get("direction", "")).split("+"))
    received_count = sum(1 for j in jobs if "received" in str(j.get("direction", "")).split("+"))
    rate         = round((active_count + offers) / total * 100) if total else 0
    days_left    = (TERM_START_DATE - dt.date.today()).days

    print("\n" + "─" * 60)
    print(f"  Total:         {total}")
    print(f"  Sent:          {sent_count}")
    print(f"  Received:      {received_count}")
    print(f"  Waiting:       {applied}")
    print(f"  Active:        {active_count}")
    print(f"  Rejected:      {rejected}")
    print(f"  Offers:        {offers}")
    print(f"  Needs review:  {needs_review}")
    print(f"  Active/offer rate: {rate}%")
    print(f"  Days to Sep 1: {days_left}")
    print("─" * 60)
    print(f"\n  Saved → {JOBS_OUT}\n")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Job Application Tracker")
    parser.add_argument("--refresh", action="store_true", help="Re-fetch emails (ignore cache)")
    parser.add_argument("--status",  action="store_true", help="Print stats from saved jobs.json")
    parser.add_argument("--test-ai", action="store_true", help="Run one tiny Gemini/Grok classification test")
    parser.add_argument("--verify-rules", action="store_true",
                        help="Run built-in classify_by_rules assertions")
    parser.add_argument("--verify-fixtures", action="store_true",
                        help="Run CLASSIFICATION_FIXTURES (rules + guardrails)")
    parser.add_argument("--start-date", default=SCAN_START_DATE.isoformat(),
                        help="First email date to scan, inclusive (YYYY-MM-DD)")
    parser.add_argument("--max-candidates", type=int, default=MAX_CANDIDATE_EMAILS,
                        help="Maximum Gmail candidate messages to inspect")
    args = parser.parse_args()
    if args.verify_fixtures:
        sys.exit(0 if _verify_classification_fixtures() else 1)
    if args.verify_rules:
        _verify_classify_by_rules()
        print("classify_by_rules: OK")
        return

    start_date = parse_start_date(args.start_date)

    if args.status:
        if not JOBS_OUT.exists():
            sys.exit("No data yet. Run: python JobAppAITracker.py")
        print_results(merge_duplicate_jobs(json.loads(JOBS_OUT.read_text())))
        return

    config  = load_config()
    if args.test_ai:
        test_ai_classifiers(config)
        return

    if not config_value(config, "gemini_api_key") and not config_value(config, "grok_api_key", "xai_api_key", "groq_api_key"):
        print("⚠️  No Gemini or Grok API key configured — using keyword fallback only.")
    service = gmail_auth()
    query = build_gmail_query(start_date)

    # Fetch or use cache
    emails = None if args.refresh else load_email_cache(start_date)
    if emails is None:
        emails = fetch_emails(service, query, start_date, args.max_candidates)
        save_email_cache(emails, start_date, query)

    if not emails:
        print(f"⚠️  No job application emails found from {display_date(start_date)} onward.")
        print("    The filter looks for confirmation/acknowledgement emails.\n")

    # Classify
    print(f"🤖 Classifying {len(emails)} emails...\n")
    class_cache = load_class_cache()
    jobs = []
    for i, email in enumerate(emails):
        preview = email["subject"][:50]
        print(f"  [{i+1}/{len(emails)}] {preview}...", end="", flush=True)
        cached = class_cache.get(email["id"])
        if cached:
            result = apply_guardrails(dict(cached), email)
            base_via = str(result.get("_via", "cache")).replace(":cached", "")
            result["_via"] = f"{base_via}:cached"
        else:
            result = classify(email, config)
            if result.get("_via") != "fallback":
                class_cache[email["id"]] = {
                    "company": result.get("company"),
                    "role": result.get("role"),
                    "status": result.get("status"),
                    "confidence": result.get("confidence"),
                    "status_scores": result.get("status_scores"),
                    "summary": result.get("summary"),
                    "location": result.get("location"),
                    "_via": result.get("_via"),
                    "_signals": result.get("_signals", []),
                }
                save_class_cache(class_cache)
        result.update({"subject": email["subject"], "from": email["from"],
                        "rawDate": email["date"], "emailId": email["id"],
                        "direction": email.get("direction", "received")})
        jobs.append(result)
        icon = ICONS.get(result["status"], "❓")
        print(f" → {icon} {result['status']} ({result['company']})")
        time.sleep(0.15)  # gentle rate limiting

    before_dedupe = len(jobs)
    jobs = merge_duplicate_jobs(jobs)
    if len(jobs) != before_dedupe:
        print(f"\n🔁 Deduplicated {before_dedupe - len(jobs)} follow-up/duplicate emails\n")

    JOBS_OUT.write_text(json.dumps(jobs, indent=2))
    print_results(jobs)

def _verify_classify_by_rules():
    convergint = {
        "subject": "Thank you for applying to Convergint",
        "body": (
            "Thank you for applying. If your qualifications align with our needs, "
            "we will reach out to schedule an interview at a later time."
        ),
        "snippet": "Thank you for applying. If your qualifications align",
        "from": "Convergint Careers <noreply@convergint.com>",
        "direction": "received",
    }
    r1 = classify_by_rules(convergint)
    assert r1 and r1["status"] == "applied" and r1["_via"] == "rules", r1

    rejection = {
        "subject": "Update on your application",
        "body": "Unfortunately, we will not be moving forward with your application.",
        "snippet": "we will not be moving forward",
        "from": "HR <hr@example.com>",
        "direction": "received",
    }
    r2 = classify_by_rules(rejection)
    assert r2 and r2["status"] == "rejected", r2

    interview = {
        "subject": "Interview invitation",
        "body": "Please schedule your interview tomorrow using the link below.",
        "snippet": "schedule your interview tomorrow",
        "from": "Recruiting <recruit@example.com>",
        "direction": "received",
    }
    r3 = classify_by_rules(interview)
    assert r3 and r3["status"] == "interviewing", r3

def _fixture_email(fixture):
    email = {
        "id": fixture.get("id"),
        "subject": fixture["subject"],
        "body": fixture["body"],
        "snippet": fixture["snippet"],
        "from": fixture["from"],
        "direction": fixture["direction"],
    }
    if fixture.get("date"):
        email["date"] = fixture["date"]
    return email

def _verify_classification_fixtures():
    print("Classification fixtures (rules + guardrails):\n")
    failed = 0
    for fixture in CLASSIFICATION_FIXTURES:
        email = _fixture_email(fixture)
        rules = classify_by_rules(email)
        final = classify_fixture(email)
        expected = fixture["expected_status"]
        actual = final.get("status")
        rules_hit = rules is not None
        rules_status = rules.get("status") if rules else None
        ok = actual == expected
        company_note = ""
        if "expected_company" in fixture:
            expected_company = fixture["expected_company"]
            actual_company = final.get("company")
            company_ok = actual_company == expected_company
            ok = ok and company_ok
            company_note = f" company={actual_company!r} expected_company={expected_company!r}"
        tag = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(
            f"  [{tag}] {fixture['id']}: expected={expected} actual={actual}{company_note} "
            f"(rules_hit={rules_hit}, rules_status={rules_status})"
        )
    print(f"\n{len(CLASSIFICATION_FIXTURES) - failed}/{len(CLASSIFICATION_FIXTURES)} passed")
    return failed == 0

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--verify-rules", "--verify-fixtures"):
        if sys.argv[1] == "--verify-fixtures":
            sys.exit(0 if _verify_classification_fixtures() else 1)
        _verify_classify_by_rules()
        print("classify_by_rules: OK")
    else:
        main()
