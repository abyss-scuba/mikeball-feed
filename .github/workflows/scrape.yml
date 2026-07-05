name: Refresh Mike Ball Availability

on:
  workflow_dispatch:
  schedule:
    - cron: "0 8 * * *"
      timezone: "Australia/Sydney"

jobs:
  scrape:
    runs-on: ubuntu-latest

    steps:
      - name: Check out repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install Playwright + browser deps
        run: |
          pip install --upgrade pip
          pip install playwright
          python -m playwright install chromium
          npx --yes playwright install-deps chromium

      - name: Run scraper rolling 4–26 weeks window
        run: |
          python scripts/scrape.py --window --out mikeball_availability.json

      - name: Show summary
        run: |
          echo "Wrote JSON:"
          ls -l mikeball_availability.json
          head -n 40 mikeball_availability.json || true

      - name: Commit and push if changed
        uses: EndBug/add-and-commit@v9
        with:
          add: "mikeball_availability.json"
          message: "chore: refresh Mike Ball availability [skip ci]"
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
