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

PRIORITY_EMOJI = {
    "Highest": ":red_circle:",
    "High":    ":large_orange_circle:",
    "Medium":  ":large_yellow_circle:",
    "Low":     ":white_circle:",
    "Lowest":  ":white_circle:",
}


def load_state() -> tuple[str, set]:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return data["last_checked"], set(data.get("reported_ids", []))
    dt = datetime.now(timezone.utc) - timedelta(hours=24)
    return dt.strftime("%Y-%m-%d %H:%M"), set()


def save_state(timestamp: str, reported_ids: set) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "last_checked": timestamp,
        "reported_ids": list(reported_ids)[-1000:],
    }))


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
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack error: {data.get('error')}")


def main() -> None:
    last_checked, reported_ids = load_state()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    print(f"[QA] Checking for bugs since: {last_checked}")
    all_issues = fetch_issues(last_checked)
    new_issues = [i for i in all_issues if i["key"] not in reported_ids]

    print(f"[QA] Found {len(all_issues)} bug(s), {len(new_issues)} not yet reported")

    if new_issues:
        post_to_slack(new_issues)
        print(f"[QA] Posted to #{SLACK_CHANNEL}")

    reported_ids.update(i["key"] for i in new_issues)
    save_state(now, reported_ids)
    print(f"[QA] State saved: {now}")


if __name__ == "__main__":
    main()
