# Morning Briefing

Automated daily news briefing delivered to your phone via push notification.

Runs on GitHub Actions at 5:30 AM UK time every day. No laptop, no Chrome, no desktop app needed.

## How it works

1. Fetches top stories from BBC, CNBC, and TechCrunch RSS feeds
2. Sends headlines to Claude AI (Anthropic API) to write a concise briefing
3. Publishes the formatted article to Telegraph
4. Sends a push notification via ntfy.sh with headlines + link

## Setup

1. Fork or create this repo on GitHub
2. Go to **Settings > Secrets and variables > Actions**
3. Add a repository secret: `ANTHROPIC_API_KEY` = your key from https://console.anthropic.com
4. The workflow runs automatically at ~5:30 AM UK time daily
5. You can also trigger it manually from **Actions > Morning Briefing > Run workflow**

## ntfy setup

Install the [ntfy app](https://ntfy.sh) on your phone and subscribe to the topic `con-hillier-morning-briefing`.
