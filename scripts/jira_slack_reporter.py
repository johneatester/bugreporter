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

CS_REPORTERS = [
    "dominique@everaccountable.com",
    "miabajan@everaccountable.com",
    "pauline@everaccountable.com",
    "zakson@everaccountable.com",
]

PIPELINES = [
    {
        "name": "qa",
        "reporters": QA_REPORTERS,
        "issue_types": ["Bug"],
        "channel": "qa-team-bugs-reported",
        "label": "Bug",
    },
    {
        "name": "cs",
        "reporters": CS_REPORTERS,
        "issue_types": ["Bug", "Task"],
        "channel": "cs-team-bug-task-reported",
        "label": "Bug/Task",
    },
]

STATE_FILE = Path("state/last_checked.json")

PRIORITY_EMOJI = {
    "Highest": ":red_circle:",
    "High":    ":large_orange_circle:",
    "Medium":  ":large_yellow_circle:",
    "Low":     ":white_circle:",
    "Lowest":  ":white_circle:",
}


def load_state() -> dict:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return {
            "last_checked": data["last_checked"],
            "qa_reported_ids": set(data.get("qa_reported_ids", [])),
            "cs_reported_ids": set(data.get("cs_reported_ids", [])),
        }
    dt = datetime.now(timezone.utc) - timedelta(hours=24)
    return {
        "last_checked": dt.strftime("%Y-%m-%d %H:%M"),
        "qa_reported_ids": set(),
        "cs_reported_ids": set(),
    }


def save_state(timestamp: str, qa_ids: set, cs_ids: set) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "last_checked": timestamp,
        "qa_reported_ids": list(qa_ids)[-1000:],
        "cs_reported_ids": list(cs_ids)[-1000:],
    }))


def fetch_issues(since: str, reporters: list, issue_types: list) -> list:
    reporter_jql = ", ".join(f'"{r}"' for r in reporters)
    projects_jql = ", ".join(PROJECTS)
    types_jql = ", ".join(issue_types)
    jql = (
        f'project in ({projects_jql}) '
        f'AND issuetype in ({types_jql}) '
        f'AND reporter in ({reporter_jql}) '
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


def format_slack_blocks(issues: list, label: str) -> list:
    count = len(issues)
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":bug: {count} New {label}{'s' if count != 1 else ''} Reported",
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


def post_to_slack(issues: list, channel: str, label: str) -> None:
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={
            "channel": channel,
            "text": f":bug: {len(issues)} new {label.lower()}(s) reported in Jira",
            "blocks": format_slack_blocks(issues, label),
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack error on channel {channel}: {data.get('error')}")


def main() -> None:
    state = load_state()
    last_checked = state["last_checked"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    print(f"Checking for issues since: {last_checked}")

    for pipeline in PIPELINES:
        name = pipeline["name"]
        reported_ids = state[f"{name}_reported_ids"]

        all_issues = fetch_issues(last_checked, pipeline["reporters"], pipeline["issue_types"])
        new_issues = [i for i in all_issues if i["key"] not in reported_ids]

        print(f"[{name.upper()}] Found {len(all_issues)} issue(s), {len(new_issues)} not yet reported")

        if new_issues:
            post_to_slack(new_issues, pipeline["channel"], pipeline["label"])
            print(f"[{name.upper()}] Posted to #{pipeline['channel']}")

        reported_ids.update(i["key"] for i in new_issues)

    save_state(now, state["qa_reported_ids"], state["cs_reported_ids"])
    print(f"State saved: {now}")


if __name__ == "__main__":
    main()
