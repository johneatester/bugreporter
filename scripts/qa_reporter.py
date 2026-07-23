import os
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

JIRA_BASE_URL = "https://everaccountable.atlassian.net"
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]

PROJECTS = ["DROID", "WEBX", "IOSX", "MOP", "MC", "BB", "WIN", "CRX"]

QA_REPORTERS = [
    "miabajan@everaccountable.com",
    "john@everaccountable.com",
    "glicerio@everaccountable.com",
    "elijah@everaccountable.com",
]

SLACK_CHANNEL = "qa-team-bugs-reported"
STATE_FILE = Path("state/qa_state.json")

# Re-scan window: every run also re-checks this far back and dedups via
# reported_ids, so bugs near the boundary (clock skew, timezone edges, bugs
# created mid-run) are never dropped even if last_checked drifted forward.
LOOKBACK_MINUTES = 120
# Keep enough recent IDs to cover the lookback window with wide margin.
MAX_REPORTED_IDS = 2000
REQUEST_TIMEOUT = 30

TIME_FMT = "%Y-%m-%d %H:%M"

PRIORITY_EMOJI = {
    "Highest": ":red_circle:",
    "High":    ":large_orange_circle:",
    "Medium":  ":large_yellow_circle:",
    "Low":     ":white_circle:",
    "Lowest":  ":white_circle:",
}


def _default_since() -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=24)
    return dt.strftime(TIME_FMT)


def load_state() -> tuple[str, list]:
    """Return (last_checked, reported_ids). Falls back to a 24h lookback with an
    empty ID list if the state file is missing or unreadable, rather than
    crashing the run on a corrupt/truncated cache."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return data["last_checked"], list(data.get("reported_ids", []))
        except (json.JSONDecodeError, KeyError, OSError) as e:
            print(f"[QA] WARNING: state file unreadable ({e}); defaulting to 24h lookback")
    return _default_since(), []


def save_state(timestamp: str, reported_ids: list) -> None:
    """Write state atomically (temp file + replace) so an interrupted run cannot
    leave a half-written state file behind."""
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "last_checked": timestamp,
        "reported_ids": reported_ids[-MAX_REPORTED_IDS:],
    }))
    tmp.replace(STATE_FILE)


def query_since(last_checked: str) -> str:
    """Apply the lookback buffer to last_checked to produce the JQL lower bound."""
    try:
        dt = datetime.strptime(last_checked, TIME_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"[QA] WARNING: bad last_checked '{last_checked}'; defaulting to 24h lookback")
        return _default_since()
    dt -= timedelta(minutes=LOOKBACK_MINUTES)
    return dt.strftime(TIME_FMT)


def fetch_issues(since: str) -> list:
    reporters_jql = ", ".join(f'"{r}"' for r in QA_REPORTERS)
    projects_jql = ", ".join(PROJECTS)
    jql = (
        f'project in ({projects_jql}) '
        f'AND issuetype = Bug '
        f'AND reporter in ({reporters_jql}) '
        f'AND created >= "{since}" '
        f'ORDER BY created DESC'
    )

    resp = requests.post(
        f"{JIRA_BASE_URL}/rest/api/3/search/jql",
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        json={
            "jql": jql,
            "fields": ["summary", "status", "priority", "reporter", "project", "created", "issuetype"],
            "maxResults": 50,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("issues", [])


def format_slack_blocks(issues: list) -> list:
    count = len(issues)
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":bug: {count} New Bug{'s' if count != 1 else ''} Reported",
            },
        },
        {"type": "divider"},
    ]

    for issue in issues:
        key = issue["key"]
        fields = issue["fields"]
        summary = fields["summary"]
        reporter = fields["reporter"]["displayName"]
        project = fields["project"]["name"]
        priority = fields.get("priority", {}).get("name", "Unknown")
        issue_type = fields.get("issuetype", {}).get("name", "Issue")
        emoji = PRIORITY_EMOJI.get(priority, ":white_circle:")
        url = f"{JIRA_BASE_URL}/browse/{key}"

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{emoji} *<{url}|{key}>* — {summary}\n"
                    f"Type: *{issue_type}* | Project: *{project}* | Reporter: *{reporter}* | Priority: *{priority}*"
                ),
            },
        })

    return blocks


def post_to_slack(issues: list) -> None:
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={
            "channel": SLACK_CHANNEL,
            "text": f":bug: {len(issues)} new bug(s) reported in Jira",
            "blocks": format_slack_blocks(issues),
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack error: {data.get('error')}")


def main() -> None:
    last_checked, reported_ids = load_state()
    reported_set = set(reported_ids)
    now = datetime.now(timezone.utc).strftime(TIME_FMT)
    since = query_since(last_checked)

    print(f"[QA] Checking for bugs since: {since} (last_checked={last_checked})")
    all_issues = fetch_issues(since)
    new_issues = [i for i in all_issues if i["key"] not in reported_set]

    print(f"[QA] Found {len(all_issues)} bug(s), {len(new_issues)} not yet reported")

    if new_issues:
        # If this raises, we deliberately do NOT advance state below: the next
        # run re-queries the same window and retries instead of silently
        # skipping bugs that were never delivered.
        post_to_slack(new_issues)
        reported_ids.extend(i["key"] for i in new_issues)
        print(f"[QA] Posted {len(new_issues)} bug(s) to #{SLACK_CHANNEL}")

    save_state(now, reported_ids)
    print(f"[QA] State saved: {now}")


if __name__ == "__main__":
    main()
