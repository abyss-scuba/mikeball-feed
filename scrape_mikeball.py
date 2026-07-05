name: Refresh Mike Ball Availability

on:
  workflow_dispatch:
  schedule:
    # GitHub cron is UTC. These two times cover Sydney 8am in AEST/AEDT.
    - cron: "0 21 * * *"
    - cron: "0 22 * * *"

permissions:
  contents: write

jobs:
  scrape:
    runs-on: ubuntu-latest

    steps:
      - name: Check Sydney 8am gate
        id: timegate
        run: |
          if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
            echo "run_scrape=true" >> "$GITHUB_OUTPUT"
            exit 0
          fi

          SYDNEY_HOUR=$(TZ=Australia/Sydney date +%H)
          if [ "$SYDNEY_HOUR" = "08" ]; then
            echo "run_scrape=true" >> "$GITHUB_OUTPUT"
          else
            echo "run_scrape=false" >> "$GITHUB_OUTPUT"
          fi

      - name: Check out repo
        if: steps.timegate.outputs.run_scrape == 'true'
        uses: actions/checkout@v4

      - name: Set up Python
        if: steps.timegate.outputs.run_scrape == 'true'
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install Playwright + browser deps
        if: steps.timegate.outputs.run_scrape == 'true'
        run: |
          pip install --upgrade pip
          pip install playwright
          python -m playwright install --with-deps chromium
          sudo apt-get update
          sudo apt-get install -y xvfb

      - name: Run scraper in virtual visible browser
        if: steps.timegate.outputs.run_scrape == 'true'
        run: |
          xvfb-run -a python scripts/scrape.py --window --out mikeball_availability.json --headful

      - name: Upload debug files if scraper fails
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: mikeball-debug
          path: |
            debug_screen.png
            debug_page.html
          if-no-files-found: ignore

      - name: Show summary
        if: steps.timegate.outputs.run_scrape == 'true'
        run: |
          echo "Wrote JSON:"
          ls -l mikeball_availability.json
          head -n 40 mikeball_availability.json || true

      - name: Commit and push if changed
        if: steps.timegate.outputs.run_scrape == 'true'
        uses: EndBug/add-and-commit@v9
        with:
          add: "mikeball_availability.json"
          message: "chore: refresh Mike Ball availability [skip ci]"
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
